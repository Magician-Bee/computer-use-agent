"""An isolated Hermes conversation process with parent-owned tool execution.

This transport is an integration candidate. It does not replace Run's prototype
loop until policy, durable state, verification and cancellation are connected.
It never launches Hermes's CLI or uses the user's Hermes configuration.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from scripts.hermes_worker import bootstrap_metadata, validate_initial_observation

ROOT = Path(__file__).resolve().parents[1]
HERMES_COMMIT = "646cd1b43e89920bb283dc11f38463204bcac9db"
HERMES_TREE_SHA256 = "e2b7f2d5c1ea50813d59426755ce0c801cf84c9605ab35fe2be905d13d65e4d4"
TOOL_NAMES = frozenset({"computer_observe", "computer_act", "computer_finish", "computer_ask_user"})


class HermesBridgeError(RuntimeError):
    pass


def verify_source(source: Path) -> None:
    try:
        manifest = json.loads((source.parent / f"{HERMES_COMMIT}.json").read_text())
        entries = manifest["files"]
        digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if manifest["commit"] != HERMES_COMMIT or digest != HERMES_TREE_SHA256:
            raise ValueError("manifest identity")
        seen = set()
        for path in source.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            name = path.relative_to(source).as_posix()
            if name not in entries:
                raise ValueError("untracked source entry")
            actual = {"symlink": path.readlink().as_posix()} if path.is_symlink() else {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            if actual != entries[name]:
                raise ValueError("changed source entry")
            seen.add(name)
        if seen != set(entries):
            raise ValueError("missing source entry")
    except (OSError, ValueError, KeyError, TypeError):
        raise HermesBridgeError("Hermes source export differs from the pinned tracked manifest") from None


def isolated_environment(profile: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
    env.update(HERMES_HOME=str(profile), PYTHONDONTWRITEBYTECODE="1", DO_NOT_TRACK="1")
    return env


class HermesBridge:
    def __init__(self, python: Path, source: Path | None = None):
        self.python = python.absolute()
        self.source = source or ROOT / ".tools" / "hermes" / HERMES_COMMIT
        self.process: asyncio.subprocess.Process | None = None
        self.profile: Path | None = None
        self._closing: asyncio.Task | None = None
        self._started = False
        self._stopping = False
        self._launch: asyncio.Task | None = None
        self._gateway_task: asyncio.Task | None = None
        self._cancel_gateway: Callable[[], Awaitable[None]] | None = None
        self._retain_profile = False

    async def run(self, *, task: str, model: str, base_url: str, tools: list[dict],
                  gateway: Callable[[str, dict], Awaitable[dict]], max_iterations: int = 30,
                  timeout: float = 300, max_tokens: int = 2048, context_length: int = 64000,
                  cancel_gateway: Callable[[], Awaitable[None]] | None = None,
                  retain_profile: bool = False,
                  initial_observation: dict | None = None,
                  model_gateway_token: str = "local-gateway-no-cloud-key") -> dict:
        if self._started or self._stopping:
            raise HermesBridgeError("Each bridge owns exactly one conversation")
        self._started = True
        self._cancel_gateway = cancel_gateway
        self._retain_profile = retain_profile
        try:
            initial_observation = validate_initial_observation(initial_observation)
        except ValueError as exc:
            raise HermesBridgeError(str(exc)) from None
        if not isinstance(model_gateway_token, str) or not 16 <= len(model_gateway_token) <= 512 or any(c.isspace() for c in model_gateway_token):
            raise HermesBridgeError("Model gateway token must be an explicit bounded local credential")
        if not 64000 <= context_length <= 1_000_000:
            raise HermesBridgeError("This pinned Hermes requires a model gateway with at least 64000 context tokens")
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise HermesBridgeError("The candidate bridge accepts an explicit loopback model gateway only")
        names = {item.get("name") for item in tools}
        if names != TOOL_NAMES or len(tools) != len(TOOL_NAMES):
            raise HermesBridgeError("Only the four parent-owned computer gateway tools may be registered")
        if (self.source / ".env").exists() or not (self.source / "run_agent.py").is_file():
            raise HermesBridgeError("Use the tracked-only Hermes export without a source .env")
        if not self.python.is_file():
            raise HermesBridgeError("Hermes Python runtime is unavailable")
        verify_source(self.source)
        profiles = ROOT / ".runtime" / "hermes"
        profiles.mkdir(parents=True, exist_ok=True)
        self.profile = Path(tempfile.mkdtemp(prefix="run-", dir=profiles))
        configuration = {"model": {"context_length": context_length}, "compression": {"enabled": False},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "sessions": {"write_json_snapshots": False},
            "agent": {"environment_probe": False, "api_max_retries": 1},
            "tool_loop_guardrails": {"enabled": True}}
        (self.profile / "config.yaml").write_text(json.dumps(configuration))
        stderr_path = self.profile / "worker.log"
        try:
            with stderr_path.open("wb") as stderr:
                self._launch = asyncio.create_task(asyncio.create_subprocess_exec(str(self.python), "-I", "-B",
                    str(ROOT / "scripts" / "hermes_worker.py"), str(self.source),
                    cwd=self.profile, env=isolated_environment(self.profile),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr,
                    limit=1_048_576))
                self.process = await asyncio.shield(self._launch)
            if self._stopping:
                raise asyncio.CancelledError
            async with asyncio.timeout(timeout):
                await self._send({"task": task, "model": model, "base_url": base_url,
                    "tools": tools, "max_iterations": max_iterations, "max_tokens": max_tokens,
                    "model_gateway_token": model_gateway_token, "initial_observation": initial_observation})
                last_sequence = 0
                ready = None
                while True:
                    try:
                        line = await self.process.stdout.readline()
                    except ValueError:
                        raise HermesBridgeError("Hermes worker exceeded the IPC message limit") from None
                    if self._stopping:
                        raise asyncio.CancelledError
                    if not line:
                        raise HermesBridgeError("Hermes worker exited before returning a result; inspect its isolated log")
                    try:
                        message = json.loads(line)
                    except (ValueError, TypeError):
                        raise HermesBridgeError("Hermes worker returned invalid protocol data") from None
                    if not isinstance(message, dict):
                        raise HermesBridgeError("Hermes worker returned a non-object protocol message")
                    if message.get("kind") == "ready":
                        tool_names = message.get("tool_names")
                        if (ready is not None or not isinstance(tool_names, list)
                                or not all(isinstance(name, str) for name in tool_names)
                                or len(tool_names) != len(TOOL_NAMES) or set(tool_names) != TOOL_NAMES):
                            raise HermesBridgeError("Hermes exposed unexpected tools")
                        ready = message
                    elif message.get("kind") == "tool":
                        name, arguments, sequence = message.get("name"), message.get("arguments"), message.get("sequence")
                        if (ready is None or not isinstance(name, str) or name not in TOOL_NAMES
                                or not isinstance(arguments, dict) or type(sequence) is not int
                                or sequence != last_sequence + 1):
                            raise HermesBridgeError("Hermes tool request failed gateway admission")
                        last_sequence = sequence
                        if self._stopping:
                            raise asyncio.CancelledError
                        self._gateway_task = asyncio.create_task(gateway(name, arguments))
                        try:
                            result = await self._gateway_task
                        finally:
                            self._gateway_task = None
                        if self._stopping:
                            raise asyncio.CancelledError
                        await self._send({"sequence": sequence, "result": result})
                    elif message.get("kind") == "result":
                        if ready is None:
                            raise HermesBridgeError("Hermes returned a result without initializing its tool contract")
                        if not all(type(message.get(field)) is bool for field in ("failed", "interrupted", "hermes_completed")):
                            raise HermesBridgeError("Hermes omitted its execution outcome")
                        return {**message, "ready": ready, "tool_calls": last_sequence,
                                "parent_bootstrap": bootstrap_metadata(initial_observation),
                                "completion_verified": False, "profile": str(self.profile) if retain_profile else None}
                    else:
                        raise HermesBridgeError("Hermes worker failed its protocol or initialization")
        finally:
            await self.close()

    async def _send(self, message: dict):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def close(self):
        self._stopping = True
        if self._closing is None:
            self._closing = asyncio.create_task(self._close_process())
        await asyncio.shield(self._closing)

    async def _close_process(self):
        try:
            gateway = self._gateway_task
            if gateway and not gateway.done():
                gateway.cancel()
            if self._cancel_gateway:
                await self._cancel_gateway()
            if gateway:
                await asyncio.gather(gateway, return_exceptions=True)
        finally:
            await self._terminate_worker()
            if self.profile and not self._retain_profile:
                shutil.rmtree(self.profile)

    async def _terminate_worker(self):
        if self._launch:
            try:
                self.process = await asyncio.shield(self._launch)
            except Exception:
                return
        if not self.process or self.process.returncode is not None:
            return
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self.process.wait(), 3)
        except asyncio.TimeoutError:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
            await self.process.wait()

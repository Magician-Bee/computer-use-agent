export class ApiError extends Error {
  constructor(message: string, public readonly status: number) { super(message); this.name = 'ApiError'; }
}

async function responseError(response: Response): Promise<ApiError> {
  let message = `請求失敗（${response.status}）`;
  try {
    const error = await response.json();
    const detail = error.detail || error.message;
    if (typeof detail === 'string') message = detail;
    else if (Array.isArray(detail)) message = detail.map((item: { msg?: string }) => item.msg || '輸入格式錯誤').join('；');
  } catch { /* Preserve the HTTP status when the server does not return JSON. */ }
  return new ApiError(message, response.status);
}

export async function api<T>(path: string, body?: unknown, signal?: AbortSignal, method?: 'GET' | 'POST' | 'DELETE'): Promise<T> {
  const requestMethod = method || (body === undefined ? 'GET' : 'POST');
  const response = await fetch(`/api${path}`, {
    method: requestMethod,
    headers: requestMethod === 'GET' ? {} : { 'Content-Type': 'application/json', 'X-ComputerUse': '1' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

export async function downloadArtifact(path: string, filename: string): Promise<void> {
  const response = await fetch(`/api${path}`);
  if (!response.ok) throw await responseError(response);
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a');
  link.href = url; link.download = filename;
  document.body.appendChild(link); link.click(); link.remove();
  // Revoking synchronously can cancel the download in some browsers.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

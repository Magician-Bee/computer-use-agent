"""Real Chromium regression for cursor rendering under enforced Trusted Types."""
import base64
import io

import pytest
from PIL import Image

from server.drivers import BrowserDriver
from server.schemas import Action


@pytest.mark.parametrize("prior_observation", [False, True])
async def test_strict_trusted_types_navigation_observation_and_real_pointer(prior_observation):
    driver = BrowserDriver(headless=True)
    try:
        await driver.start()
        if prior_observation:
            await driver.observe()  # Existing cursor handle belongs to old document.
        await driver._context.route("http://trusted-types.test/**", lambda route: route.fulfill(
            status=200, headers={"Content-Type": "text/html", "Content-Security-Policy":
                "require-trusted-types-for 'script'; trusted-types 'none'"}, body="""
            <style>body{background:white}button{position:absolute;left:400px;top:200px;width:120px;height:60px}</style>
            <button id="save">Save</button><script>
            window.saved=0; window.moves=[];
            document.addEventListener('pointermove', e=>moves.push({x:e.clientX,y:e.clientY,t:performance.now()}));
            document.getElementById('save').addEventListener('click',()=>saved++);
            </script>"""))
        await driver.execute(Action(type="navigate", url="http://trusted-types.test/strict"))
        # Prove enforcement is active; the fix must not bypass CSP or create a policy.
        assert await driver._page.evaluate("""() => {
            try { document.createElement('div').attachShadow({mode:'closed'}).innerHTML='<b>blocked</b>'; }
            catch(e) { return e instanceof TypeError; }
            return false;
        }""") is True
        observed = await driver.observe()
        target = next(e['id'] for e in observed['elements'] if e['text'] == 'Save')
        await driver.execute(Action(type="click", target=target))
        assert await driver._page.evaluate('saved') == 1
        moves = await driver._page.evaluate('moves')
        assert len(moves) >= 15 and moves[-1]['t'] - moves[0]['t'] >= 250
        assert (moves[-1]['x'], moves[-1]['y']) == (460, 230)
        observed = await driver.observe()
        assert observed['physical_input_untouched'] is True
        assert observed['cursor']['physical_os_pointer'] is False
        assert len(observed['elements']) == 1
        picture = Image.open(io.BytesIO(base64.b64decode(observed['image'].split(',', 1)[1]))).convert('RGB')
        assert sum(r > 90 and b > 150 and g < 130 for r,g,b in
                   picture.crop((460,230,482,260)).get_flattened_data()) > 30
        assert await driver._page.locator('[data-computeruse-cursor]').count() == 1
        # New document must recreate the pointer without reviving the old handle.
        await driver.execute(Action(type="navigate", url="http://trusted-types.test/second"))
        again = await driver.observe()
        assert again['url'].endswith('/second') and again['agent_cursor_available'] is True
        assert await driver._page.locator('[data-computeruse-cursor]').count() == 1
    finally:
        await driver.close()

"""Real Chromium consent controls over owned transport fixtures; no live Google."""
import argparse
import json
from pathlib import Path
import shutil
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    checks, errors, writes = [], [], []
    identity = 'a' * 32
    token = 'owned-private-review-token-not-a-credential'
    receipt = {'status': 'protocol_fixture_verified', 'live_google': False,
               'channel_id_sha256': '1' * 64, 'video_id_sha256': '2' * 64}

    def check(name, condition):
        assert condition, name
        checks.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shutil.which('chromium'), headless=True, args=['--no-sandbox'])
        page = browser.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request):
            path = urlparse(request.request.url).path
            body = request.request.post_data_json if request.request.method == 'POST' else None
            if body is not None:
                writes.append((path, body))
            if path == '/':
                return request.fulfill(content_type='text/html', body='<html><body><main>Owned browser fixture</main></body></html>')
            if path.endswith('/acceptance/state'):
                result = {'profiles': [{'id':'owned-profile','name':'Russian profile','channel_name':'Owned Russian channel','connected':True}],
                          'sessions': [], 'requirements': [{'ready':True,'action':'Owned readiness fixture'}], 'can_prepare':True}
            elif path.endswith('/acceptance/prepare'):
                result = {'acceptance_id':identity,'review_token':token,'preview_url':'/owned-media',
                          'review':{'channel_name':'Owned Russian channel','channel_id':'owned-channel-identity',
                                    'file_sha256':'3'*64,'subtitle_status':'Included: owned.srt',
                                    'metadata':{'title':'Owned generated Private test','visibility':'private'}}}
            elif path.endswith('/upload'):
                result = {'record':{'id':'owned-record','status':'queued'}}
            elif path.endswith('/verify'):
                result = {'status':'protocol_fixture_verified','export_ready':True,
                          'message':'Controlled protocol fixture verified; this is not live Google acceptance.','receipt':receipt}
            elif path.endswith('/export'):
                result = {'filename':'owned-acceptance.json','receipt':receipt}
            elif path == '/owned-media':
                return request.fulfill(status=204, body='')
            else:
                raise AssertionError('Unexpected external/network request: ' + path)
            request.fulfill(content_type='application/json', body=json.dumps(result))

        page.route('**/*', route)
        page.goto('https://forge.test/')
        page.add_script_tag(path=str(ROOT / 'web/publishing_acceptance_v10.js'))
        page.evaluate("window.ForgePublishingAcceptance.open('owned-profile')")
        page.locator('[data-prepare]').click()
        page.locator('[data-upload]').wait_for()
        check('Preparation does not upload', not any(path.endswith('/upload') for path, _ in writes))
        check('Review shows exact channel', 'owned-channel-identity' in page.locator('#forge-publishing-acceptance').inner_text())
        check('Review explicitly shows Private', 'private' in page.locator('#forge-publishing-acceptance').inner_text())
        check('Consent starts unchecked', not page.locator('[data-consent]').is_checked())
        check('Upload disabled before consent', page.locator('[data-upload]').is_disabled())
        page.locator('[data-close]').click()
        check('Closing a review sends no upload', not any(path.endswith('/upload') for path, _ in writes))
        page.evaluate("window.ForgePublishingAcceptance.open('owned-profile')")
        page.locator('[data-prepare]').click(); page.locator('[data-consent]').check()
        check('Consent enables reviewed upload', page.locator('[data-upload]').is_enabled())
        page.locator('[data-upload]').click(); page.locator('[data-check]').wait_for()
        uploads = [body for path, body in writes if path.endswith('/upload')]
        check('Only exact review capability and explicit approval are submitted', uploads == [{'review_token':token,'approve':True}])
        page.locator('[data-check]').click(); page.locator('[data-export]').wait_for()
        check('Fixture result is not labelled live acceptance', 'not live Google acceptance' in page.locator('#forge-publishing-acceptance').inner_text())
        check('Receipt export waits for owner action', not any(path.endswith('/export') for path, _ in writes))
        with page.expect_download() as saved:
            page.locator('[data-export]').click()
        download = saved.value
        path = args.output / 'owned-browser-receipt.json'
        download.save_as(path)
        check('Downloaded receipt retains fixture classification', json.loads(path.read_text()) == receipt)
        check('Receipt export explicitly approved', [body for endpoint, body in writes if endpoint.endswith('/export')] == [{'approve':True}])
        check('Browser has no uncaught JavaScript errors', not errors)
        browser.close()
    result = {'status':'passed','passed':len(checks),'checks':checks,'errors':errors,
              'scope':'Actual Chromium controls with owned HTTP fixtures; no live Google, native Windows, credentials or upload.'}
    (args.output / 'BROWSER_PUBLISHING_ACCEPTANCE_RESULTS.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()

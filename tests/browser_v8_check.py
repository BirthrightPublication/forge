"""Real V0.8 browser acceptance in a disposable service.

No model is installed or impersonated. Speech catalog/editor behavior is tested
against the real API; real FFmpeg processes a synthetic local tone through the
Universal Queue. This harness does not claim neural speech/GPU acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from forge import __version__
from forge.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', default=shutil.which('chromium'))
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []

    def check(label, condition=True):
        assert condition, label
        checks.append(label)
        print('PASS', label, flush=True)

    with tempfile.TemporaryDirectory(prefix='forge-v8-browser-') as temporary:
        folder = Path(temporary)
        listener = socket.socket(); listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        token = secrets.token_urlsafe(32)
        app = create_app(folder / 'data', token, port, workers=True)
        settings = app.state.store.settings()
        settings.update(automatic_update_checks=False, daily_update_check=False,
                        automatic_dependency_repairs=False, default_output=str(folder / 'exports'))
        app.state.store.set('settings', settings)
        source = folder / 'synthetic-tone.wav'
        samples = [int(math.sin(2 * math.pi * 440 * index / 24000) * 6000) for index in range(24000)]
        with wave.open(str(source), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
            audio.writeframes(struct.pack('<' + str(len(samples)) + 'h', *samples))
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                               log_level='error', access_log=False))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(150):
                if server.started: break
                time.sleep(.1)
            assert server.started, 'Disposable service did not start.'
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=args.chromium, headless=True,
                                                       args=['--no-sandbox'])
                context = browser.new_context(viewport={'width': 1536, 'height': 1100},
                                                timezone_id='America/Los_Angeles')
                page = context.new_page(); page.set_default_timeout(15000)
                page.on('pageerror', lambda error: errors.append(str(error)))

                def delayed_speech(route):
                    time.sleep(1)
                    route.continue_()

                page.route('**/assets/speech_v8.js', delayed_speech)
                page.goto(f'http://127.0.0.1:{port}/#token={token}')
                page.wait_for_selector('#dl-urls')
                check('Deferred speech extension preserves repaired ordinary startup', page.locator('.connection-error').count() == 0)
                check('Both speech production tools are registered in navigation', page.locator('#sidebar [data-view="tts"],#sidebar [data-view="audio_effects"]').count() == 2)
                page.locator('#sidebar [data-view="tts"]').click()
                page.wait_for_selector('#sp-editor')
                check('Unified writing editor and right voice library open together', page.locator('.sp-editor-column').count() == 1 and page.locator('.sp-library').count() == 1)
                initial_speech=page.evaluate("()=>api('/speech/state')")
                check('Original Onyx ID is selectable with truthful installation status', page.locator('[data-voice="am_onyx"]').count() == 1 and not next(e for e in initial_speech['engines'] if e['id']=='kokoro')['installed'] and 'available offline' not in page.locator('#sp-library-status').inner_text().lower())

                page.locator('#sp-project-name').fill('Synthetic Speech Acceptance')
                page.locator('#sp-editor').fill('A synthetic narrator begins. A second sentence stays unchanged.')
                page.wait_for_function("()=>window.ForgeSpeech.snapshot().project?.id && document.querySelector('#sp-status').textContent==='Saved'")
                project_id = page.evaluate('window.ForgeSpeech.snapshot().project.id')
                saved = app.state.speech.project(project_id)
                check('Autosave persists chapter text through authenticated real API', saved['chapters'][0]['text'].startswith('A synthetic narrator begins.'))
                page.locator('#sp-save').click()
                page.wait_for_function("()=>document.querySelector('#sp-status').textContent==='Project saved locally.'")
                check('Explicit Save advances a revision without changing project identity', app.state.speech.project(project_id)['revision'] >= 2)

                page.locator('#sp-voice-search').fill('no matching synthetic voice')
                check('Voice search filters without replacing the selected voice', page.locator('[data-voice="am_onyx"]').count() == 0 and 'Onyx' in page.locator('#sp-current-voice').inner_text())
                page.locator('#sp-voice-search').fill('Onyx')
                page.locator('[data-voice-favorite="am_onyx"]').click()
                page.wait_for_function("()=>document.querySelector('[data-voice-favorite=am_onyx]')?.getAttribute('aria-pressed')==='true'")
                check('Voice favorite is persisted, not only changed visually', any(v.get('voice') == 'am_onyx' and v.get('favorite') for v in app.state.speech.records('voices')))
                page.locator('#sp-voice-availability').select_option('favorites')
                check('Favorites filter preserves the original favorite voice', page.locator('[data-voice="am_onyx"]').count() == 1)
                page.locator('#sp-voice-availability').select_option('')

                def select_text(start, end):
                    page.locator('#sp-editor').focus()
                    page.locator('#sp-editor').evaluate('(el,range)=>{el.setSelectionRange(...range);el.dispatchEvent(new Event("select"));}', [start, end])

                select_text(2, 11)
                page.locator('#sp-emotion').click()
                page.locator('#sp-emotion-choice').select_option('curious')
                page.locator('#sp-emotion-insert').click()
                check('Emotion wraps exactly the selected text', '[[emotion:curious]]synthetic[[/emotion]]' in page.locator('#sp-editor').input_value())
                select_text(0, 0)
                page.locator('#sp-pause').click()
                page.locator('#sp-pause-ms').fill('1250')
                page.locator('#sp-pause-insert').click()
                check('Manual pause inserts at the text caret', page.locator('#sp-editor').input_value().startswith('[[pause:1250ms]]'))
                select_text(4, 4)
                page.locator('#sp-edit-marker').click()
                page.locator('#sp-marker-text').fill('[[pause:750ms]]')
                page.locator('#sp-marker-save').click()
                check('Existing script marker is editable', page.locator('#sp-editor').input_value().startswith('[[pause:750ms]]'))
                select_text(4, 4)
                page.locator('#sp-edit-marker').click()
                page.locator('#sp-marker-delete').click()
                check('Deleting a pause marker leaves the selected manuscript intact', page.locator('#sp-editor').input_value().startswith('A [[emotion:curious]]synthetic'))
                page.locator('#sp-spoken').click()
                page.wait_for_selector('#sp-spoken-result')
                check('Spoken preview interprets markers without reading them aloud', '[[' not in page.locator('#sp-spoken-result').inner_text() and 'synthetic narrator' in page.locator('#sp-spoken-result').inner_text())
                page.locator('#dialog').evaluate('el=>el.close()')
                page.locator('#sp-save').click()
                page.wait_for_function("()=>document.querySelector('#sp-status').textContent==='Project saved locally.'")
                page.screenshot(path=str(output / 'FORGE-V08-Speech-Studio.png'), full_page=True)

                page.locator('#sp-mixer').click()
                page.locator('#sp-mix-add').click()
                check('Mixer adds editable numeric weights and slider rows', page.locator('[data-mix-row]').count() == 2 and page.locator('[aria-label="Blend percentage 2"]').count() == 1)
                page.locator('[data-mix-remove="1"]').click()
                page.locator('[aria-label="Blend percentage 1"]').fill('70')
                page.locator('#sp-mix-normalize').click()
                check('Mixer normalization changes the saved weight input', float(page.locator('[aria-label="Blend percentage 1"]').input_value()) == 100)
                page.locator('#sp-mix-name').fill('Synthetic Onyx Recipe')
                page.screenshot(path=str(output / 'FORGE-V08-Voice-Mixer.png'), full_page=True)
                page.locator('#sp-mix-save').click()
                page.wait_for_function("()=>!document.querySelector('#dialog').open")
                recipes = [r for r in app.state.speech.records('voices') if r['name'] == 'Synthetic Onyx Recipe']
                check('Mixer saves source IDs and normalized weights as a custom recipe', recipes and recipes[0]['blend'] == [{'voice': 'am_onyx', 'weight': 1}])

                page.locator('#sp-engine').select_option('ming')
                page.locator('#sp-engine-options').evaluate('el=>el.closest("details").open=true')
                page.locator('#sp-task').select_option('FREEFORM')
                page.locator('#sp-engine-options').evaluate('el=>el.closest("details").open=true')
                check('Ming task switch exposes delivery instructions without reference controls', page.locator('#sp-instructions').count() == 1 and page.locator('#sp-reference-audio').count() == 0)
                page.locator('#sp-task').select_option('TTS')
                page.locator('#sp-engine-options').evaluate('el=>el.closest("details").open=true')
                check('Reference synthesis requires explicit permission and exact reference text', page.locator('#sp-consent').count() == 1 and not page.locator('#sp-consent').is_checked() and page.locator('#sp-reference-text').count() == 1 and page.locator('#sp-instructions').count() == 0)
                page.locator('#sp-engine').select_option('kokoro')

                rejected = page.evaluate("""async()=>{
                  const r=await fetch('/api/speech/records/voices',{method:'POST',headers:{'Content-Type':'application/json','X-Forge-CSRF':csrf},body:JSON.stringify({name:'Invalid cross-engine blend',engine:'ming',method:'blend',blend:[{voice:'am_onyx',weight:1}]})});
                  return r.status;
                }""")
                check('Cross-engine embedding recipes are rejected by the actual API', rejected == 400)
                rejected = page.evaluate("""async()=>{
                  const r=await fetch('/api/speech/parse',{method:'POST',headers:{'Content-Type':'application/json','X-Forge-CSRF':csrf},body:JSON.stringify({text:'[[exec:unsupported]]'})});return r.status;
                }""")
                check('Unknown script commands are rejected before synthesis', rejected == 400)

                page.locator('#sp-save').click()
                page.wait_for_function("()=>document.querySelector('#sp-status').textContent==='Project saved locally.'")
                manuscript = page.locator('#sp-editor').input_value()
                page.reload(); page.wait_for_selector('#sp-editor')
                page.locator('#sp-project').select_option(project_id)
                page.wait_for_function('(id)=>window.ForgeSpeech.snapshot().project.id===id', arg=project_id)
                check('Saved manuscript and speaker assignments restore after browser reload', page.locator('#sp-editor').input_value() == manuscript and app.state.speech.project(project_id)['speakers']['narrator']['voice'] == 'am_onyx')
                check('Voice favorite survives browser reload', page.locator('[data-voice-favorite="am_onyx"]').get_attribute('aria-pressed') == 'true')

                page.locator('#sidebar [data-view="audio_effects"]').click()
                page.wait_for_selector('#sp-fx-path')
                page.locator('#sp-fx-path').fill(str(source))
                page.locator('#sp-fx-load').click()
                page.wait_for_function("()=>document.querySelector('#sp-status').textContent.includes('Loaded')")
                check('Imported original audio has a waveform and playable source', page.locator('#sp-audio').get_attribute('src').startswith('/api/speech/source-audio') and page.locator('#sp-waveform').get_attribute('width') is not None)
                page.locator('#sp-fx-add').select_option('gain')
                page.locator('#sp-fx-add-button').click()
                page.locator('[data-effect-param="db"]').fill('6')
                page.locator('#sp-fx-add').select_option('lowpass')
                page.locator('#sp-fx-add-button').click()
                page.locator('[data-effect-up="1"]').click()
                check('Keyboard-accessible effect ordering changes the real chain', page.evaluate('window.ForgeSpeech.snapshot().chain.map(e=>e.type)') == ['lowpass', 'gain'])
                page.locator('[data-effect-remove="0"]').click()
                page.locator('#sp-fx-directory').fill(str(folder / 'exports'))
                with page.expect_response(lambda response: response.url.endswith('/api/speech/effects') and response.request.method=='POST') as submitted:
                    page.locator('#sp-fx-render').click()
                job_id=submitted.value.json()['job']['id']
                page.wait_for_function("async id=>(await api('/state')).jobs.some(job=>job.id===id&&job.status==='completed')",arg=job_id,timeout=45000)
                page.wait_for_function("()=>document.querySelector('#sp-status').textContent==='Audio verified and ready.'", timeout=45000)
                completed = [job for job in app.state.store.jobs() if job['tool'] == 'audio_effects' and job['status'] == 'completed']
                assert completed, 'Real effects queue job did not complete.'
                rendered = Path(next(item['path'] for item in completed[0]['outputs'] if item['name'].endswith('.wav')))
                def decode_samples(path):
                    # Final WAV can be 24-bit/stereo. Measure comparable mono
                    # PCM16 values through the installed real decoder.
                    raw = subprocess.check_output([shutil.which('ffmpeg'), '-v', 'error', '-i', str(path), '-f', 's16le', '-ac', '1', '-ar', '24000', 'pipe:1'])
                    return struct.unpack('<' + str(len(raw) // 2) + 'h', raw)
                processed = decode_samples(rendered)
                ratio = max(abs(value) for value in processed) / max(abs(value) for value in samples)
                check('Real queued gain processing changes amplitude by approximately six dB', 1.95 < ratio < 2.05)
                check('Rendering preserves the original file byte-for-byte', hashlib.sha256(source.read_bytes()).hexdigest() == source_hash)
                check('Completed effects audio is offered for playback/download', page.locator('#sp-audio-download').is_visible() and '/api/speech/audio?' in page.locator('#sp-audio').get_attribute('src'))
                check('A/B comparison becomes available after real processing', page.locator('#sp-audio-ab').is_enabled())
                processed_url=page.locator('#sp-audio').get_attribute('src')
                page.locator('#sp-audio-ab').click()
                check('A/B switches to the retained original audio', '/api/speech/source-audio?' in page.locator('#sp-audio').get_attribute('src'))
                page.locator('#sp-audio-ab').click()
                check('A/B switches back to the actual processed render', page.locator('#sp-audio').get_attribute('src')==processed_url)
                page.screenshot(path=str(output / 'FORGE-V08-Audio-Effects.png'), full_page=True)
                page.locator('#sp-fx-bypass').check()
                with page.expect_response(lambda response: response.url.endswith('/api/speech/effects') and response.request.method=='POST') as submitted:
                    page.locator('#sp-fx-render').click()
                bypass_id=submitted.value.json()['job']['id']
                page.wait_for_function("async id=>(await api('/state')).jobs.some(job=>job.id===id&&job.status==='completed')",arg=bypass_id,timeout=45000)
                page.wait_for_function("()=>document.querySelector('#sp-status').textContent==='Audio verified and ready.'", timeout=45000)
                completed = [job for job in app.state.store.jobs() if job['tool'] == 'audio_effects' and job['status'] == 'completed']
                bypassed = next(job for job in completed if job['payload']['options']['bypass'])
                bypass_path = Path(next(item['path'] for item in bypassed['outputs'] if item['name'].endswith('.wav')))
                bypass_samples = decode_samples(bypass_path)
                check('Bypass All exports the unprocessed sample amplitudes', max(abs(value) for value in bypass_samples) == max(abs(value) for value in samples))
                page.locator('#sp-fx-bypass').uncheck()
                with page.expect_response(lambda response: response.url.endswith('/api/speech/effects') and response.request.method=='POST') as submitted:
                    page.locator('#sp-fx-preview').click()
                preview_response=submitted.value
                assert preview_response.ok,preview_response.text()
                preview_id=preview_response.json()['job']['id']
                page.wait_for_function("async id=>(await api('/state')).jobs.some(job=>job.id===id&&job.status==='completed')",arg=preview_id,timeout=45000)
                check('Short preview uses the same real processing parameters', app.state.store.job(preview_id)['payload']['options']['chain']==completed[0]['payload']['options']['chain'])
                check('Effect jobs use the existing Universal Queue', all(job['tool'] == 'audio_effects' for job in app.state.store.jobs()))
                for width in (1536, 1000, 700):
                    page.set_viewport_size({'width': width, 'height': 1100})
                    check('Effects workspace fits desktop width ' + str(width), page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.locator('#sidebar [data-view="tts"]').click(); page.wait_for_selector('#sp-editor')
                for width in (1536, 1000, 700):
                    page.set_viewport_size({'width': width, 'height': 1100})
                    check('Speech workspace fits desktop width ' + str(width), page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.set_viewport_size({'width':1536,'height':1100})
                page.locator('#black-scripting-launcher').click()
                page.wait_for_selector('#black-add-node')
                page.locator('#black-name').fill('Synthetic Speech Production Chain')
                page.locator('#black-add-node').click()
                check('Black Scripting offers both new production operations',page.locator('[data-black-tool="tts"]').count()==1 and page.locator('[data-black-tool="audio_effects"]').count()==1)
                page.locator('[data-black-tool="tts"]').click()
                page.locator('#black-node-options [name="text"]').fill('A synthetic speech workflow fixture.')
                page.locator('#black-node-options [name="engine"]').select_option('ming')
                check('Black speech engine switch preserves a real Ming task configuration',page.evaluate('blackEditor.node.options.engine')=='ming' and page.evaluate('blackEditor.node.options.task')=='IP')
                page.locator('#black-node-options [name="engine"]').select_option('kokoro')
                speech_settings={'language':'en','style':'normal','task':'TTS','voice_effects':[{'type':'gain','params':{'db':-2}}]}
                page.locator('#black-speech-json').fill(json.dumps(speech_settings))
                page.locator('#black-confirm-node').click()
                page.wait_for_function('()=>blackEditor===null&&blackScript.nodes.length===1')
                node=page.evaluate('blackScript.nodes[0]')
                check('Black speech node captures original voice and engine-specific processing',node['tool']=='tts' and node['options']['engine']=='kokoro' and node['options']['voice']=='am_onyx' and node['options']['voice_effects']==speech_settings['voice_effects'])
                page.locator('#black-add-node').click()
                page.locator('[data-black-tool="audio_effects"]').click()
                effect_chain=[{'type':'gain','params':{'db':3}}]
                page.locator('#black-effects-chain').fill(json.dumps(effect_chain))
                page.locator('#black-confirm-node').click()
                page.wait_for_function('()=>blackEditor===null&&blackScript.nodes.length===2')
                check('Black effects node captures the ordered JSON chain',page.evaluate('blackScript.nodes[1].options.chain')==effect_chain)
                page.locator('#black-save').click()
                page.wait_for_function('()=>blackSaved.some(script=>script.name==="Synthetic Speech Production Chain")')
                # Check the authoritative saved object returned by the same API
                # backing the preset selector; no model work is executed.
                saved_script=page.evaluate('blackSaved.find(script=>script.name==="Synthetic Speech Production Chain")')
                check('Black speech production preset is persisted with both operation types',saved_script['nodes'][0]['tool']=='tts' and saved_script['nodes'][1]['tool']=='audio_effects')
                page.locator('[data-black-node="1"]').click()
                page.locator('#black-effects-chain').fill('{not valid JSON')
                page.locator('#black-confirm-node').click()
                page.wait_for_function('()=>document.querySelector("#black-node-error")?.textContent.length>0')
                check('Malformed Black effects JSON cannot silently save previous settings',page.evaluate('blackEditor!==null') and page.evaluate('blackScript.nodes[1].options.chain')==effect_chain)
                page.locator('#black-cancel-node').click()
                page.screenshot(path=str(output/'FORGE-V08-Black-Scripting.png'),full_page=True)
                check('No uncaught browser errors', not errors)
                browser.close()
        finally:
            server.should_exit = True; thread.join(timeout=15)
            app.state.store.close()
            (output / 'BROWSER_V8_RESULTS.json').write_text(json.dumps({
                'version': __version__, 'passed': len(checks), 'checks': checks, 'errors': errors,
                'platform': 'Linux Chromium', 'model_inference': 'Not performed; no synthetic installed model state.',
                'real_media': 'FFmpeg processes a harness-generated one-second PCM tone through the Universal Queue.'}, indent=2))


if __name__ == '__main__': main()

# Exercise the retry logic itself with a fake client, no network.
import pathlib
import sys, io, os, base64, logging, types
from typing import Tuple, List
logger = logging.getLogger('t'); logging.basicConfig(level=logging.WARNING)
# Resolved from THIS FILE, never from the working directory or a machine
# it was written on: the path was hardcoded to a sandbox and the suite
# could not run anywhere else.
SRC = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'utils' / 'file_extractor.py'
src = SRC.read_text(encoding='utf-8')

ns = {'os':os,'io':io,'base64':base64,'Tuple':Tuple,'List':List,'logger':logger,
      '_clean': lambda t: t.strip(), 'NO_TEXT_IN_IMAGE':'image contains no readable text',
      'OCR_MODEL':'m'}
exec(src[src.index('VISION_LONG_EDGE = int('):src.index('# What learners actually send.')], ns)
exec(src[src.index('def _b64_len'):src.index('def _fit_for_vision')], ns) if 'def _b64_len' in src[:src.index('def _fit_for_vision')] else None
exec(src[src.index('def _fit_batch('):src.index('def _extract_image(')], ns)
exec(src[src.index('def _ocr_with_claude'):src.index('# ---------- Spreadsheets')], ns)
ocr = ns['_ocr_with_claude']

class P:
    def __init__(s, name): s.name = name
class Msg:
    def __init__(s, t): s.content = [types.SimpleNamespace(type='text', text=t)]

calls = []
def make(behaviour):
    def _client_for(p):
        class C:
            class messages:
                @staticmethod
                def create(**kw):
                    n = sum(len(b['source']['data']) for b in kw['messages'][0]['content']
                            if b['type']=='image')
                    calls.append((p.name, n))
                    return behaviour(p, n)
        return C()
    return _client_for

from PIL import Image
import random
def shot(w,h):
    im=Image.new('RGB',(w,h),(250,250,250)); px=im.load(); random.seed(w)
    for i in range(0,w,2):
        for j in range(0,h,2): px[i,j]=(random.randrange(256),)*3
    b=io.BytesIO(); im.save(b,format='PNG'); return base64.b64encode(b.getvalue()).decode()
imgs=[('image/png',shot(1800,2600))]*2

fails=[]
def ok(n,c,d=''):
    print(('  ok   ' if c else '  FAIL ')+n+(('  — '+d) if d and not c else '')); 
    if not c: fails.append(n)

# 1. a gateway that 413s until the payload is small enough
class TooBig(Exception): pass
LIMIT = 900_000
def shrink_then_ok(p, n):
    if n > LIMIT: raise Exception("Error code: 413 - request too large")
    return Msg("TEXT:\nhello\n\nVISUAL:\na screenshot")
calls.clear()
ns['providers'] = lambda: [P('startupapi')]
ns['_client_for'] = make(shrink_then_ok)
import builtins
def fake_import(name, *a, **k):
    if name == 'app.services.ai_service':
        m = types.SimpleNamespace(providers=ns['providers'], _client_for=ns['_client_for']); return m
    return real_import(name, *a, **k)
real_import = builtins.__import__
builtins.__import__ = lambda n,*a,**k: (types.SimpleNamespace(providers=ns['providers'], _client_for=ns['_client_for']) if n=='app.services.ai_service' else real_import(n,*a,**k))
text, why = ocr(imgs, kind='screenshot')
ok('413 is retried smaller until it fits', text.startswith('TEXT'), why)
ok('and it took more than one attempt', len(calls) > 1, f'{len(calls)} call(s)')
ok('each retry was smaller than the last', all(calls[i][1] > calls[i+1][1] for i in range(len(calls)-1)),
   str([c[1] for c in calls]))

# 2. a non-413 error must fail over to the NEXT provider, not escape
calls.clear()
ns['providers'] = lambda: [P('startupapi'), P('anthropic')]
def gateway_dies(p, n):
    if p.name == 'startupapi': raise Exception("Error code: 503 - overloaded")
    return Msg("TEXT:\nfrom the fallback\n\nVISUAL:\nx")
ns['_client_for'] = make(gateway_dies)
text, why = ocr(imgs, kind='screenshot')
ok('a non-413 failure falls over to the next provider', 'fallback' in text, why)
ok('both providers were tried', {c[0] for c in calls} == {'startupapi','anthropic'}, str(calls))

# 3. every provider refusing returns the REASON, never a bare class name
calls.clear()
def all_dead(p, n): raise Exception("Error code: 401 - bad key")
ns['_client_for'] = make(all_dead)
text, why = ocr(imgs, kind='screenshot')
ok('total failure yields no text', text == '')
ok('and names the provider and the reason', 'anthropic' in why and '401' in why, why)

# 4. no provider configured
ns['providers'] = lambda: []
text, why = ocr(imgs, kind='screenshot')
ok('no provider is reported plainly', 'no Claude provider' in why, why)

builtins.__import__ = real_import
print(f"\nocr retry: {len(fails)} failed")
# Only exit when run directly. Under pytest this file is IMPORTED during
# collection, and a bare sys.exit there aborts the whole run with
# "INTERNALERROR> SystemExit" — one script-style test taking every other
# suite down with it.
if __name__ == "__main__":
    sys.exit(1 if fails else 0)
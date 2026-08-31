import pathlib
import sys, io, os, base64, logging
from typing import Tuple
logger = logging.getLogger('t')
# Resolved from THIS FILE, never from the working directory or a machine
# it was written on: the path was hardcoded to a sandbox and the suite
# could not run anywhere else.
SRC = pathlib.Path(__file__).resolve().parents[1] / 'app' / 'utils' / 'file_extractor.py'
src = SRC.read_text(encoding='utf-8')
ns = {'os':os,'io':io,'base64':base64,'Tuple':Tuple,'logger':logger}
start = src.index('VISION_LONG_EDGE = int(')
exec(src[start:src.index('# What learners actually send.')], ns)
exec(src[src.index('def _fit_batch('):src.index('def _extract_image(')], ns)
fit, batch, b64len = ns['_fit_for_vision'], ns['_fit_batch'], ns['_b64_len']
BUDGET, MINEDGE = ns['VISION_REQUEST_B64_MAX'], ns['VISION_MIN_LONG_EDGE']

from PIL import Image
import random
def shot(w,h):
    im = Image.new('RGB',(w,h),(250,250,252)); px = im.load(); random.seed(w+h)
    for i in range(0,w,2):
        for j in range(0,h,2): px[i,j]=(random.randrange(256),)*3
    b=io.BytesIO(); im.save(b,format='PNG'); return b.getvalue()
def enc(d): return base64.b64encode(d).decode()

fails=[]
def ok(n,c,d=''):
    print(('  ok   ' if c else '  FAIL ')+n+(('  — '+d) if d and not c else ''))
    if not c: fails.append(n)

one = [('image/png', enc(shot(1600,2400)))]
out, dropped = batch(one)
ok('one big screenshot fits the request budget', sum(len(b) for _,b in out) <= BUDGET,
   f'{sum(len(b) for _,b in out)} > {BUDGET}')
ok('one image is never dropped', dropped == 0)

# THE LIVE CASE: two screenshots on one submission -> 413
two = [('image/png', enc(shot(1800,2600))), ('image/png', enc(shot(1700,2500)))]
before = sum(len(b) for _,b in two)
out, dropped = batch(two)
after = sum(len(b) for _,b in out)
ok('two screenshots now fit one request', after <= BUDGET, f'{after} > {BUDGET}')
ok('both are still sent', dropped == 0 and len(out) == 2)
print(f'       (batch shrank {before//1024} KB -> {after//1024} KB of base64)')

# THE PDF CASE: five rasterised pages in one request
five = [('image/png', enc(shot(1700,2300))) for _ in range(5)]
out, dropped = batch(five)
ok('five PDF pages fit one request', sum(len(b) for _,b in out) <= BUDGET)
ok('pages are shrunk before any is dropped', dropped == 0 and len(out) == 5,
   f'only {len(out)} kept')
print(f'       (kept {len(out)} of 5, dropped {dropped})')
# The regression this guards: shrinking pixels alone could not fit five pages,
# because _fit_for_vision measured every page against the 4 MB PER-IMAGE cap
# and so never left PNG. Pages 3-5 of every five-page PDF were dropped in
# silence and the marker read two thirds of the work. The fix is the per-image
# SHARE, and the visible signature of it working is JPEG.
ok('a crowded batch reaches for JPEG, not just fewer pixels',
   any(mt == 'image/jpeg' for mt, _ in out),
   'still all PNG - the share cap is not reaching _fit_for_vision')

# a batch that CANNOT fit must still send something readable, never nothing
huge = [('image/png', enc(shot(3000,3000))) for _ in range(8)]
out, dropped = batch(huge, budget=200_000)
ok('an impossible batch still sends at least one image', len(out) >= 1)
ok('and reports what it dropped', dropped == len(huge) - len(out))

# never shrink past legibility
tiny = fit(shot(2000,2000), 'image/png', long_edge=MINEDGE)[0]
ok('the floor is respected', max(Image.open(io.BytesIO(tiny)).size) <= MINEDGE)

ok('a tiny image is untouched', batch([('image/png', enc(shot(400,300)))])[1] == 0)
ok('an empty batch does not explode', batch([]) == ([], 0))

print(f"\n_fit_batch: {len(fails)} failed")
# Only exit when run directly. Under pytest this file is IMPORTED during
# collection, and a bare sys.exit there aborts the whole run with
# "INTERNALERROR> SystemExit" — one script-style test taking every other
# suite down with it.
if __name__ == "__main__":
    sys.exit(1 if fails else 0)
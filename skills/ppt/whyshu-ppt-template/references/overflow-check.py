"""Verify all slides fit within 16:9 bounds (10×5.625")."""
import xml.etree.ElementTree as ET, os, sys

ns_a = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
ns_p = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
SW, SH = 10.0, 5.625

pptx_dir = sys.argv[1] if len(sys.argv) > 1 else '/tmp/pptx_check/ppt/slides'
issues = 0

for i in range(1, 99):
    f = os.path.join(pptx_dir, f'slide{i}.xml')
    if not os.path.exists(f): break
    tree = ET.parse(f)
    for sp in tree.iter(f'{ns_p}sp'):
        xfrm = sp.find(f'{ns_p}spPr/{ns_a}xfrm')
        if xfrm is None: continue
        off = xfrm.find(f'{ns_a}off'); ext = xfrm.find(f'{ns_a}ext')
        if off is None or ext is None: continue
        x = int(off.get('x', 0)) / 914400
        y = int(off.get('y', 0)) / 914400
        w = int(ext.get('cx', 0)) / 914400
        h = int(ext.get('cy', 0)) / 914400
        txt = ''
        for tt in sp.iter(f'{ns_a}t'):
            if tt.text: txt = tt.text.strip()[:40]; break
        if not txt: continue
        if x + w > SW + 0.05:
            print(f'  OVERFLOW slide {i}: RIGHT+{x+w-SW:.2f}" "{txt}"')
            issues += 1
        if y + h > SH + 0.05:
            print(f'  OVERFLOW slide {i}: BOTTOM+{y+h-SH:.2f}" "{txt}"')
            issues += 1

if issues:
    print(f'\n{i} slides, {issues} overflow issues — FIX REQUIRED')
    sys.exit(1)
else:
    print(f'{i} slides, zero overflow ✓')

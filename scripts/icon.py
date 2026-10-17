#!/usr/bin/env python3
"""Reproduce Slot Guard's geometric icon as SVG and a 512px RGB PNG (stdlib only)."""
from pathlib import Path
import struct
import zlib

root = Path(__file__).resolve().parents[1]
size = 512
background = (17, 24, 39)
shapes = [(48, 64, 416, 384, (30, 41, 59)), (80, 96, 352, 12, (52, 211, 153))]
for row in range(2):
    for column in range(4):
        x, y = 80 + column*92, 154 + row*130
        occupied = column < (3 if row == 0 else 2)
        shapes.append((x, y, 76, 84, (52,211,153) if occupied else (148,163,184)))
        if not occupied:
            shapes.append((x+7, y+7, 62, 70, (30,41,59)))
pixels = bytearray(background * (size*size))
for x,y,w,h,color in shapes:
    for py in range(y,y+h):
        start=(py*size+x)*3
        pixels[start:start+w*3]=bytes(color)*w

def chunk(kind, data):
    return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
raw=b''.join(b'\x00'+pixels[y*size*3:(y+1)*size*3] for y in range(size))
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',size,size,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw,9))+chunk(b'IEND',b'')
(root/'.claude-plugin/icon.png').write_bytes(png)
rects=[(0,0,size,size,background),*shapes]
svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">\n'
for x,y,w,h,c in rects:
    svg+=f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#{c[0]:02x}{c[1]:02x}{c[2]:02x}"/>\n'
svg+='</svg>\n'
(root/'docs/icon.svg').write_text(svg)
print('Generated .claude-plugin/icon.png and docs/icon.svg')

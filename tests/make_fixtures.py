"""Sample data: photos (JPEG, PNG, HEIC), a Live Photo pair, video + subs, archives, a big file."""
import os, sys, shutil, subprocess, zipfile, random
from PIL import Image, ImageDraw
import pillow_heif
pillow_heif.register_heif_opener()

BASE = sys.argv[1]
nas = os.path.join(BASE, "nas")
shutil.rmtree(BASE, ignore_errors=True)
photos = os.path.join(nas, "Media", "Photos", "2026 Roadtrip")
os.makedirs(photos)
os.makedirs(os.path.join(nas, "Media", "#recycle"))
os.makedirs(os.path.join(nas, "Media", "Movies", "Big Buck Test"))
os.makedirs(os.path.join(nas, "Media", "Archives"))
os.makedirs(os.path.join(nas, "Transfer Station"))
os.makedirs(os.path.join(BASE, "sftp", "Pictures"), exist_ok=True)
os.makedirs(os.path.join(BASE, "sftp", "Documents"), exist_ok=True)
os.makedirs(os.path.join(BASE, "sftp", ".Trash"), exist_ok=True)
os.makedirs(os.path.join(BASE, "ftp", "incoming"), exist_ok=True)

def pic(path, w, h, hue, fmt=None):
    img = Image.new("RGB", (w, h), (hue, 120, 255 - hue))
    d = ImageDraw.Draw(img)
    for i in range(0, w, 40):
        d.line([(i, 0), (w - i, h)], fill=(255, 255 - hue, hue), width=6)
    d.ellipse([w//3, h//3, w//3*2, h//3*2], fill=(250, 120, 40))
    img.save(path, fmt) if fmt else img.save(path)

for i in range(12):
    pic(os.path.join(photos, f"IMG_{4100+i}.JPG"), 2400, 1600, (i*20) % 255)
pic(os.path.join(photos, "Sunset Panorama.png"), 3000, 900, 40)
pic(os.path.join(photos, "IMG_5001.HEIC"), 2016, 1512, 200, "HEIF")
# Live Photo pair: still + 2s mov
subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=30",
                "-t", "2", "-pix_fmt", "yuv420p", "-c:v", "libx264", os.path.join(photos, "IMG_5001.MOV")], check=True)
movie = os.path.join(nas, "Media", "Movies", "Big Buck Test")
subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-t", "20", "-pix_fmt", "yuv420p",
                "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
                os.path.join(movie, "Big Buck Test (2026).mp4")], check=True)
with open(os.path.join(movie, "Big Buck Test (2026).en.srt"), "w") as f:
    f.write("1\n00:00:01,000 --> 00:00:04,000\nHello from Ferry\n\n2\n00:00:05,500 --> 00:00:09,000\nSubtitles <i>work</i>\n")
with open(os.path.join(movie, "Big Buck Test (2026).es.ass"), "w") as f:
    f.write("[Script Info]\nTitle: t\n\n[V4+ Styles]\nFormat: Name\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:02.00,0:00:06.50,Default,,0,0,0,,{\\i1}Hola{\\i0} desde Ferry\\Nsegunda linea\n")
arch = os.path.join(nas, "Media", "Archives")
with zipfile.ZipFile(os.path.join(arch, "Scans.zip"), "w") as z:
    z.writestr("Scans/page1.txt", "page one")
    z.writestr("Scans/deep/page2.txt", "page two")
    z.writestr("readme.txt", "zip readme")
for n in ("rar5-psw.rar", "rar3-solid.rar", "rar5-solid.rar", "rar3-comment-plain.rar", "rar5-hpsw.rar"):
    src = os.path.join("/tmp/rarfile-src/test/files", n)
    if os.path.exists(src):
        shutil.copy(src, arch)
with open(os.path.join(nas, "Transfer Station", "big.bin"), "wb") as f:
    f.write(os.urandom(64 << 20))
with open(os.path.join(nas, "Transfer Station", "notes.md"), "w") as f:
    f.write("# Notes\n\nFerry test file.\n")
# Mac side
for i in range(3):
    pic(os.path.join(BASE, "sftp", "Pictures", f"mac_{i}.jpg"), 1200, 800, 90 + i * 30)
with open(os.path.join(BASE, "sftp", "Documents", "résumé – final.txt"), "w") as f:
    f.write("unicode name test\n")
print("fixtures ok")

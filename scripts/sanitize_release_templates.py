"""移除上游示例照片，以本项目绘制的中性占位图替代；只处理已认证模板。"""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
from io import BytesIO
import hashlib
import json
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def placeholder(format):
    image = Image.new('RGB', (240, 320), '#edf0f4')
    draw = ImageDraw.Draw(image)
    draw.ellipse((76, 48, 164, 136), fill='#b5becb')
    draw.rounded_rectangle((38, 153, 202, 292), radius=64, fill='#b5becb')
    out = BytesIO()
    image.save(out, format=format)
    return out.getvalue()


def main():
    for template, part, kind in [('t001','word/media/image1.jpeg','JPEG'),('t109','word/media/image1.png','PNG')]:
        folder = ROOT/'backend/skill_toolbox/skill_defs/resume_pro/templates'/template
        source = folder/'template.docx'
        temporary = source.with_suffix('.sanitized.docx')
        with ZipFile(source) as zin, ZipFile(temporary,'w',ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = placeholder(kind) if item.filename == part else zin.read(item.filename)
                if 'thumbnail' in item.filename.lower():
                    data = placeholder('JPEG' if item.filename.endswith(('.jpg','.jpeg')) else 'PNG')
                zout.writestr(item, data)
        temporary.replace(source)
        manifest_path = folder/'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['template_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest['sample_photo'] = 'Generated neutral placeholder; scripts/sanitize_release_templates.py'
        manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(template, manifest['template_sha256'])


if __name__ == '__main__':
    main()

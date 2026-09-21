"""为 Release 收集对应 PDF 引擎源码和许可，不包含本机配置或用户数据。"""
from pathlib import Path
import hashlib
import json
import tarfile
import tomllib
from urllib.request import urlretrieve
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    'pymupdf-1.28.2.tar.gz': 'https://files.pythonhosted.org/packages/a3/fb/b6761fa2d5266f2cdb24c3b91f4023070ab7848381417678e7a289a1d52a/pymupdf-1.28.2.tar.gz',
    'mupdf-1.28.2-source.tar.gz': 'https://mupdf.com/downloads/archive/mupdf-1.28.2-source.tar.gz',
    'poppler-26.07.0.tar.xz': 'https://poppler.freedesktop.org/poppler-26.07.0.tar.xz',
}


def main():
    folder = ROOT/'backend-dist/release-sources'
    folder.mkdir(parents=True,exist_ok=True)
    notices = ROOT/'licenses'
    records = []
    for name, url in SOURCES.items():
        path = folder/name
        if not path.exists():
            cached = ROOT/'.tmp_t/release-sources'/name
            if cached.exists():
                import shutil
                shutil.copy2(cached,path)
            else:
                urlretrieve(url,path)
        with tarfile.open(path) as archive:
            for entry in archive.getmembers():
                relative = Path(entry.name)
                if not entry.isfile() or not any(relative.name.upper().startswith(key) for key in ('COPYING','LICENSE','NOTICE','FTL.TXT')):
                    continue
                if '..' in relative.parts or relative.is_absolute():
                    raise ValueError('Unsafe archive path')
                target = notices/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(archive.extractfile(entry).read())
        records.append({'file':name,'url':url,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    (notices/'sources.json').write_text(json.dumps(records,indent=2)+'\n',encoding='utf-8')
    version = tomllib.loads((ROOT/'pyproject.toml').read_text(encoding='utf8'))['project']['version']
    output=ROOT/f'backend-dist/DocKit-Resume-{version}-PDF-Sources.zip'
    with zipfile.ZipFile(output,'w',zipfile.ZIP_STORED) as archive:
        for record in records:
            archive.write(folder/record['file'],record['file'])
        archive.write(notices/'sources.json','sources.json')
    print('Source archive:',output,flush=True)


if __name__ == '__main__':
    main()

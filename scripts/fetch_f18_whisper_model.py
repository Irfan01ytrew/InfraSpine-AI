from __future__ import annotations
import hashlib, json, os
from pathlib import Path
from huggingface_hub import HfApi, snapshot_download

repo_id='Systran/faster-whisper-tiny.en'
root=Path(__file__).resolve().parents[1]
target=root/'.verification-local-review-20261027-native'/'f18-whisper-tiny-en'
if target.exists():
    raise SystemExit('Refusing to overwrite an existing F18 model folder; inspect it first.')
os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN']='1'
info=HfApi(token=False).model_info(repo_id)
card=info.cardData or {}
if card.get('license')!='mit':
    raise SystemExit('The expected public model license metadata was not present.')
revision=info.sha
if not revision or len(revision)!=40:
    raise SystemExit('The model repository did not return a pinned Git commit.')
local=snapshot_download(repo_id=repo_id,revision=revision,local_dir=str(target),allow_patterns=['config.json','model.bin','tokenizer.json','vocabulary.txt','README.md'])
required=['config.json','model.bin','tokenizer.json','vocabulary.txt']
rows=[]
for name in required:
    path=Path(local)/name
    if not path.is_file():
        raise SystemExit(f'Model artifact missing: {name}')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    rows.append({'file':name,'bytes':path.stat().st_size,'sha256':digest})
manifest={'repository':repo_id,'revision':revision,'license':'mit','files':rows}
manifest_path=target/'verification-manifest.json'
manifest_path.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
print(f'F18_MODEL_READY repository={repo_id} revision={revision} total_bytes={sum(row["bytes"] for row in rows)}')
print(f'F18_MODEL_MANIFEST={manifest_path}')

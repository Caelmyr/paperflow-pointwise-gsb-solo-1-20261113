import io
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import fitz
import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.core import Store
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-acceptance')
    app = create_app(directory, demo=False)
    client = app.test_client()
    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json
    collection = call('POST', '/collections', {'name': '验收集合', 'category': '测试'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '验收文档'})
    for name in ('demo_1.png', 'demo_2.png'):
        with (ROOT / 'demo' / name).open('rb') as handle:
            response = client.post('/api/documents/' + document['id'] + '/import', data={'files': (io.BytesIO(handle.read()), name)}, headers=HEADERS)
        assert response.status_code == 201, response.json
    document = call('GET', '/documents/' + document['id'])
    yield app, client, call, collection, document, directory
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown(wait=True)


def wait_task(call, task_id):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        task = call('GET', '/tasks/' + task_id)
        if task['status'] in ('completed', 'partial', 'failed', 'cancelled', 'interrupted'):
            return task
        time.sleep(.05)
    pytest.fail('任务超时')


def test_01_collections_import_and_validation(work):
    app, client, call, collection, document, directory = work
    assert len(document['page_ids']) == 2
    updated = call('PATCH', '/collections/' + collection['id'], {'name': '中英文档案', 'category': '验收'})
    assert updated['name'] == '中英文档案'
    updated = call('PATCH', '/documents/' + document['id'], {'name': '数字化验收', 'category': '资料'})
    assert updated['category'] == '资料'
    response = client.post('/api/documents/' + document['id'] + '/import', data={'files': (io.BytesIO(b'bad image'), 'broken.png')}, headers=HEADERS)
    assert response.status_code == 207
    assert response.json['failures'] and not response.json['page_ids']
    assert client.post('/api/collections', json={'name':'x'}).status_code == 403
    assert client.post('/api/collections', json={'name':'x'}, headers={**HEADERS,'Origin':'https://evil.example'}).status_code == 403
    for page in document['pages']:
        image = client.get('/api/pages/' + page['id'] + '/image?variant=thumbnail')
        assert image.status_code == 200
        assert image.mimetype == 'image/jpeg'
    sources = call('GET', '/sources?document_id=' + document['id'])
    assert len(sources) == 2
    assert all(call('GET', '/sources/' + source['id'] + '/verify')['valid'] for source in sources)


def test_02_perspective_rotation_deskew(work):
    app, client, call, collection, document, directory = work
    photo = call('POST', '/documents', {'collection_id':collection['id'],'name':'透视验收'})
    call('POST', '/demo', {'document_id':photo['id'],'sample':'photo'})
    page = call('GET', '/documents/' + photo['id'])['pages'][0]
    edges = call('GET', '/pages/' + page['id'] + '/edges')
    assert edges['detected'] and len(edges['points']) == 4
    changed = call('POST', '/pages/' + page['id'] + '/process', {'operation':'perspective','params':{'points':edges['points']}})
    assert changed['current'] != changed['original']
    rotated = call('POST', '/pages/' + page['id'] + '/process', {'operation':'rotate','params':{'angle':90}})
    assert rotated['width'] == changed['height'] and rotated['height'] == changed['width']
    call('POST', '/pages/' + page['id'] + '/process', {'operation':'rotate','params':{'angle':-90}})
    skew = call('GET', '/pages/' + page['id'] + '/skew')
    assert 'angle' in skew
    call('POST', '/pages/' + page['id'] + '/process', {'operation':'deskew','params':{}})
    restored = call('POST', '/pages/' + page['id'] + '/restore', {})
    assert restored['current'] == restored['original']


def test_03_enhancement_modes_noise_regions(work):
    app, client, call, collection, document, directory = work
    page_id = document['page_ids'][0]
    for operation, params in [
        ('illumination', {'strength':.6}),
        ('color', {'mode':'gray'}),
        ('color', {'mode':'binary','threshold':160}),
        ('color', {'mode':'adaptive','block_size':35,'constant':12}),
        ('denoise', {'area':5,'median':1,'region':[100,100,500,500]}),
    ]:
        result = call('POST', '/pages/' + page_id + '/process', {'operation':operation,'params':params})
        assert result['history'][-1]['operation'] == operation
    region = call('POST', '/pages/' + page_id + '/regions', {'name':'正文','kind':'text','region':[100,100,500,500]})
    response = client.get('/api/pages/' + page_id + '/regions/' + region['id'])
    assert Image.open(io.BytesIO(response.data)).size == (500,500)
    cropped = call('POST', '/pages/' + page_id + '/process', {'operation':'crop','params':{'region':[100,100,500,500]}})
    assert (cropped['width'],cropped['height']) == (500,500)
    restored = call('POST', '/pages/' + page_id + '/restore', {})
    assert restored['width'] == 1400
    bad = client.post('/api/pages/' + page_id + '/process', json={'operation':'color','params':{'block_size':4}}, headers=HEADERS)
    assert bad.status_code == 400
    conflict = client.post('/api/pages/' + page_id + '/process', json={'operation':'rotate','revision':-1}, headers=HEADERS)
    assert conflict.status_code == 409


def test_04_real_chinese_english_ocr_edit_search(work):
    app, client, call, collection, document, directory = work
    assert call('GET', '/health')['ocr']['available']
    task = call('POST', '/tasks', {'page_ids':document['page_ids'],'operation':'ocr','params':{'language':'chi_sim+eng'}})
    task = wait_task(call, task['id'])
    assert task['status'] == 'completed', task
    page_id = document['page_ids'][0]
    page = call('GET', '/pages/' + page_id)
    assert 'PaperFlow' in page['ocr']['text']
    assert '文档' in page['ocr']['text']
    assert page['ocr']['confidence'] > 60
    assert all('confidence' in box for box in page['ocr']['boxes'])
    word = page['ocr']['boxes'][0]
    edited = call('PATCH', '/pages/' + page_id + '/ocr', {'boxes':[{'id':word['id'],'text':'人工校订标题'}]})
    assert edited['ocr']['boxes'][0]['text'] == '人工校订标题'
    edited = call('PATCH', '/pages/' + page_id + '/ocr', {'text':edited['ocr']['text']+'\n人工修改已保存 PaperFlow Verified 2026'})
    assert edited['ocr']['edited']
    hits = call('GET', '/search?q=Verified')
    assert hits and hits[0]['page_id'] == page_id


def test_05_multi_page_order_copy_move_split_merge(work):
    app, client, call, collection, document, directory = work
    order = list(reversed(document['page_ids']))
    call('POST', '/documents/' + document['id'] + '/reorder', {'page_ids':order})
    copied = call('POST', '/pages/copy', {'page_ids':[order[0]],'target_id':document['id'],'position':1})['page_ids'][0]
    doc = call('GET','/documents/'+document['id'])
    assert doc['page_ids'] == [order[0],copied,order[1]]
    split = call('POST','/documents/'+document['id']+'/split',{'page_ids':[copied],'name':'拆分验收'})
    assert split['page_ids'] == [copied]
    merged = call('POST','/documents/merge',{'document_ids':[document['id'],split['id']],'name':'合并验收'})
    assert len(merged['page_ids']) == 3
    assert [p['original'] for p in merged['pages']] == [app.extensions['store'].get('pages',p)['original'] for p in order+[copied]]
    moved = call('POST','/pages/move',{'page_ids':[copied],'target_id':document['id'],'position':1})
    assert moved['page_ids'][1] == copied
    call('POST','/pages/delete',{'page_ids':[copied]})
    assert call('GET','/documents/'+document['id'])['page_ids'] == order


def test_06_exports_searchable_pdf_copyable_order(work):
    app, client, call, collection, document, directory = work
    current = call('GET','/documents/'+document['id'])
    for kind in ('txt','pdf','searchable_pdf','images','zip'):
        result = call('POST','/documents/'+document['id']+'/export',{'kind':kind})
        payload = client.get('/api/exports/'+result['id']+'/download')
        assert payload.status_code == 200 and len(payload.data)>100
        verified = call('GET','/exports/'+result['id']+'/verify')
        assert verified['valid'], verified
        if kind == 'searchable_pdf':
            pdf = fitz.open(stream=payload.data,filetype='pdf')
            assert len(pdf) == 2
            texts = [page.get_text() for page in pdf]
            assert 'Verified' in texts[1]
            assert 'Verified' not in texts[0]
            assert pdf[1].search_for('Verified')
            assert any('\u4e00' <= character <= '\u9fff' for character in texts[1])
            pdf.close()
        elif kind == 'zip':
            archive = zipfile.ZipFile(io.BytesIO(payload.data))
            metadata = json.loads(archive.read('metadata.json'))
            assert metadata['order'] == current['page_ids']
            assert all(not Path(page['current']).is_absolute() for page in metadata['pages'])
            assert metadata['sources']
            imported = app.extensions['archives'].import_document(app.extensions['store'].resolve(result['path']), collection['id'], 'ZIP往返验收')
            assert len(imported['pages']) == 2
            assert 'Verified' in imported['pages'][1]['ocr']['text']


def test_07_batch_partial_failure_cancel(work):
    app, client, call, collection, document, directory = work
    missing = 'page_' + 'f'*32
    partial = call('POST','/tasks',{'page_ids':[document['page_ids'][0],missing],'operation':'rotate','params':{'angle':0}})
    completed = wait_task(call,partial['id'])
    assert completed['status'] == 'partial' and completed['failed'] == 1 and completed['succeeded'] == 1
    assert completed['results'][1]['code'] == 'not_found'
    # The first page was deliberately processed above; refresh its OCR before export tests elsewhere.
    copies = call('POST','/pages/copy',{'page_ids':document['page_ids'],'target_id':document['id']})['page_ids']
    long_task = call('POST','/tasks',{'page_ids':document['page_ids']+copies,'operation':'ocr','params':{}})
    cancelled = call('POST','/tasks/'+long_task['id']+'/cancel',{})
    done = wait_task(call,long_task['id'])
    assert done['status'] == 'cancelled'
    assert done['done'] < done['total']
    call('POST','/pages/delete',{'page_ids':copies})


def test_08_restart_and_associations(work):
    app, client, call, collection, document, directory = work
    original = app.extensions['store'].snapshot()
    reopened = Store(directory).snapshot()
    assert reopened['documents'] == original['documents']
    assert reopened['pages'] == original['pages']
    for page in reopened['pages'].values():
        assert page['document_id'] in reopened['documents']
        assert app.extensions['store'].resolve(page['original']).is_file()
    assert any('Verified' in p['ocr']['text'] for p in reopened['pages'].values() if p.get('ocr'))


def test_09_advanced_business_services(work):
    app, client, call, collection, document, directory = work
    page_id = document['page_ids'][0]
    for endpoint in ('analysis','layout','comparison','recommendations','provenance'):
        assert call('GET','/pages/'+page_id+'/'+endpoint)
    metadata = call('PATCH','/documents/'+document['id']+'/metadata',{'author':'验收人','tags':['合同','中文'],'date':'2026-11-12'})
    assert metadata['metadata']['tags'] == ['合同','中文']
    query = call('POST','/catalog/query',{'tag':'合同'})
    assert query['total'] == 1
    note = call('POST','/pages/'+page_id+'/notes',{'text':'检查段落'})
    assert call('PATCH','/pages/'+page_id+'/notes/'+note['id'],{'resolved':True})['resolved']
    checkpoint = call('POST','/documents/'+document['id']+'/checkpoints',{'name':'验收快照'})
    assert not call('GET','/checkpoints/'+checkpoint['id']+'/diff')['changed']
    restored = call('POST','/checkpoints/'+checkpoint['id']+'/restore',{'name':'快照恢复验收'})
    assert len(restored['page_ids']) == 2
    for kind in ('hocr','alto','csv','markdown','tiff'):
        assert call('POST','/documents/'+document['id']+'/interchange',{'kind':kind})['bytes'] > 0
    for kind in ('html','json','csv'):
        assert call('POST','/documents/'+document['id']+'/report',{'kind':kind})['bytes'] > 0
    backup = call('POST','/backups',{})
    assert call('GET','/backups/'+backup['id']+'/verify')['valid']
    assert call('GET','/audit/integrity?deep=1')['valid']
    assert call('GET','/audit/storage')['total_bytes'] > 0
    flow = call('POST','/pipelines',{'name':'轻量验收','steps':[{'operation':'contrast','params':{'contrast':1,'brightness':0}}]})
    run = call('POST','/pipelines/'+flow['id']+'/run',{'page_ids':[page_id]})
    deadline = time.monotonic()+30
    while time.monotonic()<deadline:
        run = call('GET','/pipeline-runs/'+run['id'])
        if run['status'] in ('completed','failed','partial'):
            break
        time.sleep(.05)
    assert run['status'] == 'completed', run


def test_10_relative_launch_and_renamed_copy(work,tmp_path):
    moved = tmp_path/'renamed-paperflow-workbench'
    shutil.copytree(ROOT,moved,ignore=shutil.ignore_patterns('.venv','.tools','data','__pycache__','.pytest_cache','*.zip'))
    code = "import sys,tempfile;sys.path.insert(0,sys.argv[1]);from paperflow.web import create_app;a=create_app(tempfile.mkdtemp(),demo=False);c=a.test_client();assert c.get('/').status_code==200;assert c.get('/static/app.js').status_code==200;assert c.get('/static/style.css').status_code==200;print('renamed-copy-and-cwd-ok');a.extensions['scheduler'].shutdown();a.extensions['pipelines'].executor.shutdown()"
    process = subprocess.run([sys.executable,'-c',code,str(moved)],cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert process.returncode == 0, process.stderr
    assert 'renamed-copy-and-cwd-ok' in process.stdout


def test_11_line_count_over_5000():
    sys.path.insert(0,str(ROOT/'scripts'))
    from count_lines import report
    assert report()['total'] > 5000

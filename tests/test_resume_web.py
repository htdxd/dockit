import asyncio
import time
import zipfile

import pytest
pytest.importorskip('fastapi', reason='网站测试需 uv run --extra web pytest')
from fastapi.testclient import TestClient

from skill_toolbox.web.app import create_app, validate_upload
from skill_toolbox.web.runner import JobRunner
from skill_toolbox.web.state import Store


class FakeRunner(JobRunner):
    async def run(self, job):
        self.current = job['id']
        self.store.update(job['id'], 'waiting', questions=[{'id': 'detail', 'label': '项目结果是什么？', 'type': 'textarea'}])
        self.done = asyncio.Event()
        await self.done.wait()
        self.current = None

    async def send(self, kind, **payload):
        if kind == 'answer_questions':
            root = self.store.directory / 'jobs' / self.current / 'outputs'
            root.mkdir(exist_ok=True)
            path = root / 'resume.docx'
            path.write_bytes(b'transport-test-only')
            extra = root / 'resume.pdf'
            extra.write_bytes(b'internal-preview-only')
            self.event(self.current, {'type':'task_completed','artifacts':[str(extra), str(path)]}, root)
        else:
            self.store.update(self.current, 'cancelled')
        self.done.set()

    async def cancel(self):
        await self.send('cancel_task')


@pytest.fixture
def site(tmp_path):
    config = {'data_dir':str(tmp_path), 'public_origin':'http://testserver', 'secure_cookie':False,
              'provider':{'api_key':'a-private-provider-key'}, 'mineru_key':'another-private-token'}
    app = create_app(config, runner_factory=FakeRunner)
    store = app.state.store
    code = store.invite(2)
    with TestClient(app, headers={'Origin':'http://testserver'}) as client:
        yield client, store, code, app


def login(client, code):
    result = client.post('/api/login', json={'code':code})
    assert result.status_code == 200
    assert 'HttpOnly' in result.headers['set-cookie']


def submit(client, **extra):
    return client.post('/api/tasks', data={'user_prompt':'基本信息与真实经历', 'consent':'true', **extra},
                       files=[('files',('note.txt',b'original information','text/plain'))])


def wait_status(client, job, status):
    deadline = time.monotonic()+3
    while time.monotonic()<deadline:
        result = client.get('/api/tasks/'+job).json()
        if result['status']==status:
            return result
        time.sleep(.02)
    pytest.fail(f'not {status}: {result}')


def test_login_csrf_and_no_secret_endpoints(site):
    client, store, code, _ = site
    assert client.get('/api/tasks').status_code == 401
    assert client.post('/api/login', json={'code':code}, headers={'Origin':'https://attacker.example'}).status_code == 403
    login(client, code)
    assert client.get('/api/me').json()['quota'] == 2
    for path in ['/server.json','/runtime/python-packages.json','/api/providers','/docs','/openapi.json']:
        assert client.get(path).status_code == 404
    assert client.post('/api/logout',json={}).status_code == 200
    assert client.get('/api/tasks').status_code == 401


def test_complete_questions_download_and_isolation(site):
    client, store, code, app = site
    login(client, code)
    result = submit(client)
    assert result.status_code == 200, result.text
    job = result.json()['id']
    snapshot = wait_status(client,job,'waiting')
    assert snapshot['questions'][0]['id']=='detail'
    assert 'payload' not in snapshot and 'owner' not in snapshot
    assert client.post(f'/api/tasks/{job}/answers',json={'detail':'完成了项目'}).status_code == 200
    result = wait_status(client,job,'completed')
    assert [f['name'] for f in result['artifacts']] == ['resume.docx']
    assert client.get(f'/api/tasks/{job}/files/0').content==b'transport-test-only'
    assert client.get(f'/api/tasks/{job}/files/-1').status_code==404
    assert client.post(f'/api/tasks/{job}/feedback',json={'rating':4,'text':'很好'}).status_code==200
    login(client,store.invite(2))
    assert client.get('/api/tasks').json()==[]
    for path in [f'/api/tasks/{job}',f'/api/tasks/{job}/files/0']:
        assert client.get(path).status_code==404
    assert client.post(f'/api/tasks/{job}/cancel',json={}).status_code==404


def test_submission_restrictions_and_file_names(site):
    client, store, code, _ = site
    login(client,code)
    for extra in [{'skill_id':'ppt-master'},{'provider':'malicious'},{'template_id':'t026'},{'output_dir':'C:/secret'},{'consent':'false'}]:
        assert submit(client,**extra).status_code==400
    assert client.post('/api/tasks',data={'user_prompt':'','consent':'true'}).status_code==400
    response=client.post('/api/tasks',data={'consent':'true'},files=[('files',('../../secret.txt',b'one')),('files',('other.txt',b'two'))])
    assert response.status_code==200,response.text
    job=store.jobs()[0]
    assert job['payload']['files']==['1.txt','2.txt']
    root=store.directory/'jobs'/job['id']/'uploads'
    assert (root/'1.txt').read_bytes()==b'one'
    assert (root/'2.txt').read_bytes()==b'two'
    assert submit(client).status_code==400


def test_cancel_refunds_once(site):
    client,store,code,_=site
    login(client,code)
    job=submit(client).json()['id']
    wait_status(client,job,'waiting')
    assert client.get('/api/me').json()['used']==1
    client.post(f'/api/tasks/{job}/cancel',json={})
    wait_status(client,job,'cancelled')
    client.post(f'/api/tasks/{job}/cancel',json={})
    assert client.get('/api/me').json()['used']==0


def test_store_restart_and_queue_quota(tmp_path):
    store=Store(tmp_path)
    code=store.invite(1)
    owner=store.owner(store.login(code))
    store.create(owner,'a',{},0)
    other=store.owner(store.login(store.invite(1)))
    with pytest.raises(ValueError,match='队列'):
        store.create(other,'b',{},0)
    store.update('a','failed')
    store.update('a','failed')
    assert store.quota(owner)['used']==0
    store.create(owner,'b',{},0)
    store.update('b','completed')
    with pytest.raises(ValueError,match='次数'):
        store.create(owner,'c',{},0)
    assert Store(tmp_path).get('b')['status']=='completed'


def test_runner_rejects_external_download_and_redacts(site,tmp_path):
    client,store,code,app=site
    runner=app.state.runner
    assert 'private' not in runner.clean('a-private-provider-key another-private-token')
    external=tmp_path/'secret.docx'
    external.touch()
    with pytest.raises(RuntimeError,match='路径'):
        runner.event('x',{'type':'task_completed','artifacts':[str(external)]},tmp_path/'outputs')


def test_docx_upload_validation(tmp_path):
    path=tmp_path/'test.docx'
    path.write_bytes(b'not zip')
    with pytest.raises(ValueError,match='损坏'):
        validate_upload(path)
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('word/document.xml','<xml/>')
        archive.writestr('word/vbaProject.bin',b'macro')
    with pytest.raises(ValueError,match='宏'):
        validate_upload(path)


def test_restart_marks_interrupted_and_purges_expired(tmp_path):
    store=Store(tmp_path)
    user=store.owner(store.login(store.invite(5)))
    store.create(user,'old',{},20)
    store.update('old','completed')
    (tmp_path/'jobs/old').mkdir(parents=True)
    (tmp_path/'jobs/old/private.txt').write_text('private')
    with store.db() as db:
        db.execute('UPDATE jobs SET created=? WHERE id=?',(time.time()-9*86400,'old'))
    store.create(user,'interrupted',{},20)
    store.update('interrupted','running')
    app=create_app({'data_dir':str(tmp_path),'provider':{},'mineru_key':'test'},runner_factory=FakeRunner)
    with TestClient(app) as client:
        assert client.get('/healthz').json()['ok']
        assert store.get('interrupted')['status']=='failed'
        assert store.quota(user)['used']==1
        assert not (tmp_path/'jobs/old').exists()
        assert store.get('old') is None


def test_benchmark_no_codes_no_quota_and_parallel_routing(tmp_path):
    config={'data_dir':str(tmp_path),'public_origin':'http://testserver','secure_cookie':False,
            'provider':{},'mineru_key':'test','benchmark_mode':True,'max_concurrent_tasks':2}
    app=create_app(config,runner_factory=FakeRunner)
    with TestClient(app,headers={'Origin':'http://testserver'},client=('127.0.0.1',50000)) as client:
        assert client.get('/api/me').json()['benchmark_mode'] is True
        first=submit(client).json()['id']
        second=submit(client).json()['id']
        third=submit(client).json()['id']
        wait_status(client,first,'waiting')
        wait_status(client,second,'waiting')
        assert client.get('/api/tasks/'+third).json()['status']=='queued'
        assert len({r.current for r in app.state.runners})==2
        client.post(f'/api/tasks/{second}/answers',json={})
        wait_status(client,second,'completed')
        wait_status(client,third,'waiting')
        assert client.get('/api/tasks/'+first).json()['status']=='waiting'
        assert client.get('/api/me').json()['used']==3  # 自动会话原始 quota=1，压测没有次数门。
        client.post(f'/api/tasks/{first}/cancel',json={})
        client.post(f'/api/tasks/{third}/cancel',json={})
        wait_status(client,first,'cancelled')
    with TestClient(app,client=('203.0.113.1',50000)) as remote:
        assert remote.get('/api/me').status_code==403


def test_loadtest_artifact_validation_and_failure_denominator(tmp_path):
    from skill_toolbox.web.loadtest import check_artifact, summarize
    import io
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as archive:
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>测0001</w:t></w:r></w:p></w:document>')
    check_artifact(buffer.getvalue(),'resume.docx','测0001')
    with pytest.raises(ValueError,match='串单'):
        check_artifact(buffer.getvalue(),'resume.docx','测0002')
    summary=summarize([{'ok':True,'seconds':10},{'ok':False,'seconds':.1}],20)
    assert summary['success_rate']==.5
    assert summary['p95_seconds']==10
    assert summary['throughput_per_hour']==180

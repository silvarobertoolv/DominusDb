import pytest
from app import app, state

@pytest.fixture
def client():
    app.config['TESTING']=True
    state.update(source=None,target=None,plan=None,job=None)
    with app.test_client() as client:
        client.get('/')
        with client.session_transaction() as sess: client.csrf=sess['csrf']
        yield client


def post(client,path,data=None): return client.post(path,json=data or {},headers={'X-CSRF-Token':client.csrf})


def test_home_and_no_credentials_in_cookie(client):
    response=client.get('/')
    assert response.status_code==200 and b'SQLBridge' in response.data
    with client.session_transaction() as session: assert list(session)==['csrf']


def test_csrf_and_host_checks(client):
    assert client.post('/api/inventory',json={}).status_code==403
    assert client.get('/',headers={'Host':'evil.example'}).status_code==400


def test_missing_connections_and_stale_plan_blocked(client):
    assert post(client,'/api/inventory').status_code==400
    assert post(client,'/api/plan',{'tables':['dbo.t']}).status_code==400
    assert post(client,'/api/run',{'plan_id':'old','confirmation':'MIGRAR'}).status_code==400


def test_jobs_lock_mutations(client):
    state['job']={'status':'running'}
    assert post(client,'/api/inventory').status_code==409
    assert post(client,'/api/connect/source',{}).status_code==409


def test_validation_and_errors_do_not_echo_secrets(client):
    response=post(client,'/api/connect/source',{'password':'secret-test'})
    assert response.status_code==400 and b'secret-test' not in response.data

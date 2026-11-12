import base64,json
import httpx
from click.testing import CliRunner
from unittest.mock import patch
from quicksilverpro.machines import MachinesClient
from quicksilverpro.box import box
from quicksilverpro.mcp import dispatch,SCHEMAS

MID='a'*12
JID='b'*32

def client(handler):
    return MachinesClient(key='qsm_fixture',transport=httpx.MockTransport(handler))

def test_long_execute_streams_and_ignores_many_heartbeats():
    def handler(request):
        assert request.headers['accept']=='text/event-stream'
        events=[{'event':'stdout','seq':1,'data':'hello'},{'event':'exit','seq':2,'exec_id':JID,'exit_code':0,'signal':None,'timed_out':False,'truncated':False,'duration_ms':1}]
        data=b': keepalive\n\n'*10000+b''.join(('event: '+e['event']+'\ndata: '+json.dumps(e)+'\n\n').encode() for e in events)
        return httpx.Response(200,content=data)
    with client(handler) as c:
        result=c.execute(MID,argv=['sleep','65'],timeout_seconds=65)
        assert result['stdout']=='hello' and result['exit_code']==0

def test_job_body_paths_and_follow_reconnection():
    calls=[]
    def handler(req):
        calls.append(req)
        if req.method=='POST':
            body=json.loads(req.content)
            assert body['timeout_seconds']==86400
            return httpx.Response(201,json={'id':JID,'state':'running'})
        if req.url.params.get('follow')=='true':
            end=len(calls)>2
            data={'data':base64.b64encode(b'hi').decode(),'next_offset':2 if not end else 4,'end_offset':2 if not end else 4,'state':'exited' if end else 'running'}
            return httpx.Response(200,text=': keepalive\n\nevent: logs\ndata: '+json.dumps(data)+'\n\n')
        return httpx.Response(200,json={'jobs':[]})
    with client(handler) as c:
        assert c.job_start(MID,command='sleep 1000')['id']==JID
        assert len(list(c.job_follow(MID,JID)))==2
        assert calls[-1].url.params['offset']=='2'

def test_cli_job_start_and_mcp_dispatch():
    from unittest.mock import MagicMock
    fake=MagicMock();fake.__enter__.return_value=fake;fake.job_start.return_value={'id':JID}
    with patch('quicksilverpro.box.MachinesClient',return_value=fake):
        result=CliRunner().invoke(box,['job','start',MID,'--env','FOO=bar','--','sleep','1000'])
        assert result.exit_code==0,result.output
        args=fake.job_start.call_args.kwargs
        assert args['argv']==['sleep','1000'] and args['env']=={'FOO':'bar'}
    with patch('quicksilverpro.mcp.MachinesClient',return_value=fake):
        assert dispatch('box_job_start',{'mid':MID,'command':'sleep 1000'})=={'id':JID}
    assert set(k for k in SCHEMAS if k.startswith('box_job_'))=={'box_job_start','box_job_list','box_job_status','box_job_logs','box_job_kill'}

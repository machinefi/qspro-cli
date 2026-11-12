import json
import inspect
import httpx
import pytest
from click.testing import CliRunner
from quicksilverpro.cli import main
from quicksilverpro.machines import MachinesClient, MachineError
from quicksilverpro.mcp import dispatch

MID='a'*12

def cli_runner():
    return CliRunner(mix_stderr=False) if 'mix_stderr' in inspect.signature(CliRunner).parameters else CliRunner()

def test_clone_cli_request_and_name_resolution(mock_api):
    def handler(request):
        if request.method=='GET':return httpx.Response(200,json={'machines':[{'id':MID,'name':'configured'}],'limit':3})
        return httpx.Response(201,json={'machines':[{'id':'b'*12}],'error':None})
    calls=mock_api(handler)
    result=cli_runner().invoke(main,['box','clone','configured','--from-snapshot','ready','--name','worker','--count','2','--accept-spend','--json'])
    assert result.exit_code==0,result.output
    post=calls[-1];assert post.url.path=='/v1/machines/'+MID+'/clone'
    body=json.loads(post.content);assert body['count']==2 and body['from_snapshot']=='ready' and body['name']=='worker'
    assert json.loads(result.stdout)['result']['machines'][0]['id']=='b'*12

def test_clone_retry_preserves_request_id_and_partial_results(mock_api):
    calls=mock_api(lambda r:httpx.Response(200,json={'machines':[{'id':'b'*12}],'error':{'status':409,'detail':'Account limit reached.'}}))
    with MachinesClient() as client:
        first=client.clone(MID);second=client.clone(MID)
    assert first['request_id']==second['request_id']
    assert json.loads(calls[0].content)['request_id']==json.loads(calls[1].content)['request_id']
    result=cli_runner().invoke(main,['box','clone',MID,'--accept-spend','--json'])
    assert result.exit_code==1
    assert json.loads(result.stdout)['result']['machines'][0]['id']=='b'*12

def test_clone_mcp_spend_gate_and_public_request(mock_api):
    pytest.importorskip('jsonschema')
    calls=mock_api(lambda r:httpx.Response(201,json={'machines':[],'error':None}))
    args={'mid':MID,'request_id':'clone-request-00000001'}
    with pytest.raises(MachineError,match='accept_spend'):dispatch('box_clone',args)
    assert calls==[]
    result=dispatch('box_clone',{**args,'accept_spend':True,'count':2,'from_snapshot':'ready'})
    assert result['error'] is None
    assert json.loads(calls[-1].content)['count']==2

@pytest.mark.parametrize('count',[True,0,65])
def test_invalid_count_never_requests(mock_api,count):
    calls=mock_api(lambda r:httpx.Response(500))
    with MachinesClient() as client,pytest.raises(MachineError):client.clone(MID,count=count)
    assert calls==[]

def test_mcp_partial_failure_keeps_admitted_ids(mock_api):
    pytest.importorskip('mcp')
    import anyio
    from mcp.shared.memory import create_connected_server_and_client_session
    from quicksilverpro.mcp import create_server
    mock_api(lambda r:httpx.Response(201,json={'machines':[{'id':'b'*12}],'error':{'status':409,'detail':'Account limit reached.'}}))
    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as session:
            result=await session.call_tool('box_clone',{'mid':MID,'count':2,'request_id':'clone-request-00000001','accept_spend':True})
            assert result.isError
            assert result.structuredContent['result']['machines'][0]['id']=='b'*12
    anyio.run(scenario)

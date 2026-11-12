import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock
import httpx
import pytest
from quicksilverpro import ssh
from quicksilverpro.machines import MachineError

MID='a'*12


def test_config_idempotent_preserves_user_blocks_and_quotes(tmp_path):
    directory=tmp_path/'ssh folder';directory.mkdir()
    config=directory/'config';config.write_text('Host *\n  ServerAliveInterval 60\nHost personal\n  User me\n')
    rows=[{'id':MID,'name':'work; $(bad) " host\nProxyCommand false'}]
    ssh.write_config(directory,rows,'/a path/qsp')
    first=config.read_text();ssh.write_config(directory,rows,'/a path/qsp');assert config.read_text()==first
    assert first.count('# QSP machine '+MID+' BEGIN')==1
    assert 'Host personal\n  User me' in first
    assert 'StrictHostKeyChecking yes' in first
    assert first.index('Host qsp-')<first.index('Host *')
    assert 'ProxyCommand \'/a path/qsp\' box ssh-proxy '+MID in first
    result=subprocess.run(['ssh','-G','-F',str(config),'qsp-'+MID],capture_output=True,text=True,check=True)
    assert 'user user' in result.stdout
    assert 'hostkeyalias qsp-'+MID in result.stdout
    assert config.stat().st_mode & 0o777==0o600


def test_config_rejects_symlinks_and_bad_id(tmp_path):
    config=tmp_path/'config';target=tmp_path/'personal';target.write_text('unchanged');config.symlink_to(target)
    with pytest.raises(MachineError):ssh.write_config(tmp_path,[{'id':MID,'name':'work'}])
    assert target.read_text()=='unchanged'
    config.unlink()
    with pytest.raises(ValueError):ssh.write_config(tmp_path,[{'id':'injected\nHost *','name':'work'}])


def test_pin_rotation_preserves_other_machine(tmp_path):
    ssh.pin_hosts(tmp_path,MID,'172.30.1.2 ssh-ed25519 ZmFrZQ==')
    other='b'*12;ssh.pin_hosts(tmp_path,other,'ip ssh-ed25519 b3RoZXI=')
    ssh.pin_hosts(tmp_path,MID,'ip ssh-ed25519 bmV3')
    lines=(tmp_path/'qsp_known_hosts').read_text().splitlines()
    assert len(lines)==2 and 'qsp-'+MID+' ssh-ed25519 bmV3' in lines
    with pytest.raises(ValueError):ssh.pin_hosts(tmp_path,MID,'')


def test_setup_reuses_private_key_and_registers_public_only(monkeypatch,tmp_path):
    directory=tmp_path/'.ssh';directory.mkdir();(directory/'qsp_ed25519').write_text('fixture private')
    monkeypatch.setattr(ssh.subprocess,'check_output',lambda *a,**k:b'ssh-ed25519 ZmFrZQ==')
    run=Mock();monkeypatch.setattr(ssh.subprocess,'run',run)
    requests=[]
    def request(path='',body=None):
        requests.append((path,body));return {'id':MID,'name':'work'} if path=='/'+MID else {}
    monkeypatch.setattr(ssh,'request',request)
    ssh.configure(MID);ssh.configure(MID)
    run.assert_not_called();assert all('fixture private' not in str(r) for r in requests)
    assert requests[0][0]=='/ssh-keys'


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize('names', [('workspace', MID), ('Work space', 'work-space')])
def test_setup_commands_resolve_to_intended_machines(monkeypatch,tmp_path,capsys,reverse,incremental,names):
    directory=tmp_path/'.ssh';directory.mkdir();(directory/'qsp_ed25519').write_text('fixture private')
    monkeypatch.setattr(ssh.subprocess,'check_output',lambda *a,**k:b'ssh-ed25519 ZmFrZQ==')
    monkeypatch.setattr(ssh,'request',lambda *a,**k:{})
    rows=[{'id':MID,'name':names[0]},{'id':'b'*12,'name':names[1]}]
    if reverse:rows.reverse()
    commands=[]
    for _ in range(2):
        for batch in ([[row] for row in rows] if incremental else [rows]):
            monkeypatch.setattr(ssh,'resolve',lambda value:batch)
            assert ssh.configure()==batch
            output=capsys.readouterr();assert output.out==''
            lines=output.err.splitlines()
            assert len(lines)==len(batch)
            for line,row in zip(lines,batch):
                assert line=='Connect with: ssh qsp-'+row['id']
                commands.append((line.removeprefix('Connect with: ssh '),row['id']))
            # Previously emitted commands must survive incremental reconciliation.
            for host,mid in commands:
                result=subprocess.run(['ssh','-G','-F',str(directory/'config'),host],capture_output=True,text=True,check=True)
                effective=dict(line.split(' ',1) for line in result.stdout.splitlines())
                assert effective['hostname']==mid
                assert effective['hostkeyalias']=='qsp-'+mid
                assert effective['proxycommand'].endswith(' box ssh-proxy '+mid)


def test_proxy_resumes_and_pins_before_bridging(monkeypatch,tmp_path):
    responses=iter([{'id':MID,'name':'work','state':'Paused'}, {'state':'Paused'}, {}, {'state':'Running'},
                    {'host':'https://box.test','token':'secret-ticket','host_keys':'ip ssh-ed25519 ZmFrZQ=='}])
    paths=[]
    def request(path='',body=None):paths.append((path,body));return next(responses)
    monkeypatch.setattr(ssh,'request',request);monkeypatch.setattr(ssh.time,'sleep',lambda _:None)
    def bridge(url):
        assert 'wss://box.test/ssh/'+MID in url
        assert (tmp_path/'.ssh/qsp_known_hosts').read_text().startswith('qsp-'+MID+' ')
    monkeypatch.setattr(ssh,'bridge',bridge)
    ssh.proxy(MID)
    assert ('/'+MID+'/action/resume',{}) in paths
    assert paths[-1]==('/'+MID+'/token',{'purpose':'ssh'})


def test_binary_transport_no_tty_or_credentials(monkeypatch):
    import select
    import websockets.sync.client
    frames=[];written=[];reads=iter([b'SSH-2.0-test\r\n\x00\xff',b''])
    monkeypatch.setattr(select,'select',lambda *a:([0],[],[]))
    monkeypatch.setattr(ssh.os,'read',lambda *a:next(reads))
    monkeypatch.setattr(ssh.os,'write',lambda fd,b:(written.append(bytes(b)),len(b))[1])
    monkeypatch.setattr(ssh.sys,'stdin',Mock(fileno=lambda:0));monkeypatch.setattr(ssh.sys,'stdout',Mock(fileno=lambda:1))
    class Socket:
        def send(self,b):frames.append(b)
        def recv(self,**kw):return b'raw\x00\xff'
    @contextmanager
    def connect(url,**kw):
        assert 'authorization' not in str(kw).lower() and kw['proxy'] is None
        yield Socket()
    monkeypatch.setattr(websockets.sync.client,'connect',connect)
    ssh.bridge('wss://box.test/ssh/'+MID+'?token=secret-ticket')
    assert frames==[b'SSH-2.0-test\r\n\x00\xff'] and written==[b'raw\x00\xff']


def test_mcp_ssh_info_only_reads(mock_api):
    pytest.importorskip("jsonschema")
    from quicksilverpro.mcp import dispatch
    calls=mock_api(lambda r:httpx.Response(200,json={'id':MID,'name':'work'}))
    result=dispatch('box_ssh_info',{'mid':MID})
    assert result['setup_command']=='qsp box ssh-config '+MID
    assert len(calls)==1 and calls[0].method=='GET'

@pytest.mark.parametrize('preamble', ['ServerAliveInterval 77\nIdentitiesOnly yes\n', 'Include defaults\n', 'Match host personal\n Include defaults\nHost *\n'])
def test_effective_global_preamble_and_default_quiet_transport(tmp_path,preamble):
    (tmp_path/'defaults').write_text('ServerAliveInterval 77\nIdentitiesOnly yes\n')
    preamble=preamble.replace('Include defaults','Include '+str(tmp_path/'defaults'))
    config=tmp_path/'config';config.write_text(preamble+'Host personal\n HostName personal.example.test\n')
    def effective(host):
        return subprocess.check_output(['ssh','-G','-F',str(config),host],stderr=subprocess.DEVNULL,text=True)
    before=effective('personal')
    for _ in range(2):
        ssh.write_config(tmp_path,[{'id':MID,'name':'work'}])
        assert effective('personal')==before
    config.write_text('')
    ssh.write_config(tmp_path,[{'id':MID,'name':'work'}])
    assert 'serveraliveinterval 0\n' in effective('qsp-'+MID)

@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('incremental', [False, True])
@pytest.mark.parametrize('names', [('workspace', MID), ('Work space', 'work-space')])
def test_effective_alias_collisions_preserve_id_targets(tmp_path,reverse,incremental,names):
    rows=[{'id':MID,'name':names[0]},{'id':'b'*12,'name':names[1]}]
    if reverse:rows.reverse()
    def effective(host):
        return dict(line.split(' ',1) for line in subprocess.check_output(
            ['ssh','-G','-F',str(tmp_path/'config'),host],stderr=subprocess.DEVNULL,text=True).splitlines())
    for _ in range(2):
        if incremental:
            for row in rows:ssh.write_config(tmp_path,[row])
        else:ssh.write_config(tmp_path,rows)
        for row in rows:
            result=effective('qsp-'+row['id'])
            assert result['hostname']==row['id']
            assert result['hostkeyalias']=='qsp-'+row['id']
            assert result['proxycommand']=='qsp box ssh-proxy '+row['id']
        if names[0]=='Work space':
            assert 'proxycommand' not in effective('qsp-work-space')


def test_existing_managed_name_cannot_shadow_future_id(tmp_path):
    ssh.write_config(tmp_path,[{'id':'b'*12,'name':MID}])
    config=tmp_path/'config'
    result=subprocess.check_output(['ssh','-G','-F',str(config),'qsp-'+MID],stderr=subprocess.DEVNULL,text=True)
    assert '\nproxycommand ' not in result
    # Upgrade a pre-fix block with the colliding ID-shaped name alias.
    config.write_text(config.read_text().replace('Host qsp-'+('b'*12), 'Host qsp-'+MID+' qsp-'+('b'*12)).replace('# QSP name alias qsp-'+MID+'\n',''))
    ssh.write_config(tmp_path,[{'id':MID,'name':'workspace'}])
    result=subprocess.check_output(['ssh','-G','-F',str(config),'qsp-'+MID],stderr=subprocess.DEVNULL,text=True)
    assert 'hostname '+MID+'\n' in result

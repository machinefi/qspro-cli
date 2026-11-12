"""OpenSSH uses an authenticated binary WS as its byte transport."""
import contextlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
from .machines import MachineError, MachinesClient, safe_path, atomic_write

MID=re.compile(r'[a-f0-9]{12}')


def request(path='',body=None):
    with MachinesClient() as client:
        return client.request('GET' if body is None else 'POST','/v1/machines'+path,**({} if body is None else {'json':body}))


def resolve(value):
    if value and MID.fullmatch(value):return [request('/'+value)]
    rows=request()['machines']
    if value:
        rows=[m for m in rows if m['name']==value or alias(m)==value]
        if len(rows)!=1:raise MachineError('machine_not_found','Choose a unique machine name or its ID.',2)
    return rows


def alias(machine):
    name=re.sub('[^a-z0-9-]+','-',machine['name'].lower()).strip('-') or machine['id']
    return 'qsp-'+name


def quoted(path):
    return '"'+str(path).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def atomic(path,text):
    atomic_write(path,text,private=True)


@contextlib.contextmanager
def locked(directory):
    if os.name!='posix':raise MachineError('unsupported_platform','SSH setup currently requires macOS or Linux.',2)
    import fcntl
    directory=safe_path(directory)
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    with safe_path(directory/'.qsp-config.lock').open('a') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def write_config(directory,machines,executable='qsp'):
    with locked(directory):
        config=safe_path(directory/'config')
        text=config.read_text() if config.exists() else ''
        for machine in machines:
            mid=machine['id']
            if not MID.fullmatch(mid):raise ValueError('Invalid machine ID')
            start='# QSP machine '+mid+' BEGIN'
            end='# QSP machine '+mid+' END'
            text=re.sub(re.escape(start)+r'\n.*?'+re.escape(end)+r'\n?', '',text,flags=re.S)
            command=shlex.quote(executable).replace('%','%%')+' box ssh-proxy '+mid
            block=(f'{start}\nHost {alias(machine)} qsp-{mid}\n'
                   f'# QSP name alias {alias(machine)}\n  HostName {mid}\n  User user\n  IdentityFile {quoted(directory/"qsp_ed25519")}\n'
                   f'  IdentitiesOnly yes\n  HostKeyAlias qsp-{mid}\n'
                   f'  UserKnownHostsFile {quoted(directory/"qsp_known_hosts")}\n'
                   f'  StrictHostKeyChecking yes\n  ProxyCommand {command}\n'
                   +'Host *\n'+end+'\n')
            # Place before wildcard defaults: OpenSSH takes the first value.
            text=block+text.lstrip('\n')
        # Reconcile every managed block, including those from earlier setup.
        # ID-shaped aliases are reserved even if that machine is not set up yet.
        pattern=re.compile(r'(# QSP machine ([a-f0-9]{12}) BEGIN\n)Host ([^\n]+)\n(.*?# QSP machine \2 END\n?)',re.S)
        blocks=list(pattern.finditer(text));owners={}
        def candidates(block):
            saved=re.search(r'^# QSP name alias (qsp-[a-z0-9-]+)$',block[4],re.M)
            return [saved[1]] if saved else block[3].split()
        for block in blocks:
            for name in set(candidates(block)):owners.setdefault(name,set()).add(block[2])
        def unambiguous(block):
            mid=block[2];identity='qsp-'+mid
            names=[name for name in candidates(block) if name!=identity and
                   not MID.fullmatch(name[4:] if name.startswith('qsp-') else name) and len(owners[name])==1]
            body=block[4]
            if '# QSP name alias ' not in body:
                body=''.join('# QSP name alias '+name+'\n' for name in candidates(block) if name!='qsp-'+mid)+body
            return block[1]+'Host '+' '.join(dict.fromkeys(names+[identity]))+'\n'+body
        text=pattern.sub(unambiguous,text)
        atomic(config,text)


def pin_hosts(directory,mid,host_keys):
    if not MID.fullmatch(mid):raise ValueError('Invalid machine ID')
    lines=[]
    for line in host_keys.splitlines():
        parts=line.split()
        if len(parts)<3 or not re.fullmatch(r'(ssh-|ecdsa-)[A-Za-z0-9@._+-]+',parts[1]):raise ValueError('Invalid host key')
        from base64 import b64decode
        b64decode(parts[2],validate=True)
        lines.append('qsp-'+mid+' '+parts[1]+' '+parts[2])
    if not lines:raise ValueError('No trusted host keys')
    with locked(directory):
        path=safe_path(directory/'qsp_known_hosts')
        old=path.read_text().splitlines() if path.exists() else []
        old=[line for line in old if not line.startswith('qsp-'+mid+' ')]
        atomic(path,'\n'.join(old+lines)+'\n')


def configure(value=None):
    directory=Path.home()/'.ssh'
    with locked(directory):
        private=safe_path(directory/'qsp_ed25519')
        if not private.exists():
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(private)],check=True)
        os.chmod(private,0o600)
        public=subprocess.check_output(['ssh-keygen','-y','-f',str(private)],stdin=subprocess.DEVNULL).decode().strip()
    request('/ssh-keys',{'public_key':public,'label':'QSP CLI'})
    machines=resolve(value)
    write_config(directory,machines,shutil.which('qsp') or 'qsp')
    for machine in machines:print('Connect with: ssh qsp-'+machine['id'],file=sys.stderr)
    return machines


def proxy(value):
    machine=resolve(value)[0];mid=machine['id']
    detail=request('/'+mid)
    if detail['state'] in ('Paused','Sleeping','Queued'):
        print('Starting your QSP machine…',file=sys.stderr)
        request('/'+mid+'/action/resume',{})
    deadline=time.monotonic()+600
    while detail['state']!='Running':
        if detail['state'] in ('Error','Deleting') or time.monotonic()>deadline:raise ValueError('Machine did not start')
        time.sleep(2);detail=request('/'+mid)
    ticket=request('/'+mid+'/token',{'purpose':'ssh'})
    host=urlsplit(ticket['host'])
    if host.scheme!='https' or not host.netloc or host.username or host.password:raise ValueError('Invalid transport URL')
    pin_hosts(Path.home()/'.ssh',mid,ticket['host_keys'])
    from urllib.parse import urlencode
    url='wss://'+host.netloc+'/ssh/'+mid+'?'+urlencode({'token':ticket['token']})
    bridge(url)


def bridge(url):
    """Binary ProxyCommand transport; no TTY mode changes or stdout diagnostics."""
    import select
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosedOK
    try:
        with connect(url,open_timeout=10,close_timeout=2,max_size=65536,proxy=None) as ws:
            while True:
                if select.select([sys.stdin.fileno()],[],[],0.02)[0]:
                    data=os.read(sys.stdin.fileno(),32768)
                    if not data:return
                    ws.send(data)
                try:data=ws.recv(timeout=0.02)
                except TimeoutError:continue
                except ConnectionClosedOK:return
                if not isinstance(data,bytes):raise ValueError('Invalid transport frame')
                view=memoryview(data)
                while view:view=view[os.write(sys.stdout.fileno(),view):]
    except (KeyboardInterrupt,SystemExit):raise
    except Exception:raise MachineError('ssh_transport','SSH transport failed or disconnected. Retry the connection.') from None


def interactive(value):
    machine=configure(value)[0]
    os.execvp('ssh',['ssh','qsp-'+machine['id']])

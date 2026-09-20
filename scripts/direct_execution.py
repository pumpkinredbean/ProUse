#!/usr/bin/env python3
"""Durable command-only execution in one registered workspace per operation."""
from __future__ import annotations
import argparse, base64, fcntl, hashlib, json, os
from pathlib import Path
import re, socket, subprocess, sys, threading, tempfile, stat
from typing import Any
from codex_app_server import AppServerError, CodexAppServer
from codex_orchestrator import _atomic_json, _json_bytes, _manifest_delta, _now, _workspace_manifest
from context_store import ContextError, SECRET_CONTENT, relative_path

OP_RE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
TERMINAL={"succeeded","failed","cancelled","scope_violation","interrupted"}
class DirectExecutionError(ValueError):
 def __init__(self,code,message): super().__init__(message); self.result={"status":"error","error":{"code":code,"message":message}}
def _sha(x): return hashlib.sha256(x).hexdigest()
def _key(x): return "op_"+_sha(x.encode())[:20]
def _read(path):
 try:
  value=json.loads(path.read_text())
  if not isinstance(value,dict): raise ValueError()
  return value
 except (OSError,ValueError): raise DirectExecutionError("unavailable","Direct operation receipt is unavailable") from None
def _public(value):
 allowed={"executor_kind","workspace_id","operation_id","request_sha256","status","created_at","updated_at","started_at","completed_at","exit_code","error","worker_model","codex_thread_id","changed_paths","unexpected_changed_paths","cancel_requested","native_result","output_capped"}
 result={key:value[key] for key in allowed if key in value}
 # Buffered native output stays in the private durable receipt/files and is retrieved only by byte cursors.
 if isinstance(result.get("native_result"),dict): result["native_result"]={key:item for key,item in result["native_result"].items() if key not in {"stdout","stderr"}}
 return result
def _relative(value,root,missing=False):
 if value in (None,"", "."): return ".",root
 try: rel=relative_path(value)
 except ContextError: raise DirectExecutionError("scope_violation","Expected a workspace-relative path") from None
 target=root/rel; current=root
 for part in rel.parts:
  current/=part
  try:
   if current.is_symlink(): raise DirectExecutionError("scope_violation","Symlink paths are not available")
  except OSError: raise DirectExecutionError("unavailable","Workspace path unavailable") from None
 if not missing and not target.exists(): raise DirectExecutionError("unavailable","Workspace path unavailable")
 return rel.as_posix(),target
def _update(directory,change):
 with os.fdopen(os.open(directory/".receipt.lock",os.O_CREAT|os.O_RDWR,0o600),"a+b") as lock:
  fcntl.flock(lock,fcntl.LOCK_EX); state=_read(directory/"receipt.json"); change(state); state["updated_at"]=_now(); _atomic_json(directory/"receipt.json",state); return state
def _control(directory,request):
 path=Path(_read(directory/"receipt.json").get("control_socket", ""))
 if not path or not path.exists(): return None
 try:
  with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
   s.settimeout(1); s.connect(str(path)); s.sendall(json.dumps(request,separators=(",",":")).encode()+b"\n"); data=b""
   while not data.endswith(b"\n"):
    chunk=s.recv(8192)
    if not chunk:return None
    data+=chunk
   reply=json.loads(data)
   return reply if isinstance(reply,dict) else None
 except (OSError,ValueError): return None

def _safe_artifact_read(root, path):
 """Read a result by descriptors, never following a swapped link or linked secret file."""
 parts=relative_path(path).parts; parent=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); fd=None
 try:
  for part in parts[:-1]:
   child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);os.close(parent);parent=child
  fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent); info=os.fstat(fd)
  if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1: raise DirectExecutionError("scope_violation","Artifact must be a regular non-linked file")
  data=os.read(fd,info.st_size+1)
  if len(data)>info.st_size or SECRET_CONTENT.search(data): raise DirectExecutionError("scope_violation","Artifact content is unavailable")
  return data
 except DirectExecutionError: raise
 except OSError: raise DirectExecutionError("unavailable","Artifact unavailable") from None
 finally:
  if fd is not None:os.close(fd)
  os.close(parent)

class DirectExecutionController:
 def __init__(self,registry,state_dir=None): self.registry=registry; self.state_dir=Path(state_dir or registry.state_dir)
 def _base(self,wid): return self.state_dir/"workspaces"/wid/"direct-operations"
 def _dir(self,wid,oid): return self._base(wid)/_key(oid)
 def _resolve(self,wid,oid):
  if not isinstance(oid,str) or not OP_RE.fullmatch(oid): raise DirectExecutionError("invalid_request","operation_id must be a safe identifier")
  workspace=self.registry.workspace(wid); directory=self._dir(workspace["id"],oid)
  if not directory.is_dir(): raise DirectExecutionError("unknown_operation","Unknown direct operation")
  return workspace,directory
 def exec_command(self,workspace_id,operation_id,command,cwd=".",timeoutMs=None,outputBytesCap=None,disableTimeout=None,disableOutputCap=None,stream_stdin=True,stream_stdout_stderr=True,tty=False):
  workspace=self.registry.workspace(workspace_id)
  if not isinstance(operation_id,str) or not OP_RE.fullmatch(operation_id): raise DirectExecutionError("invalid_request","operation_id must be a safe identifier")
  if not isinstance(command,list) or not command or not isinstance(command[0],str) or not command[0] or any(not isinstance(x,str) or "\0" in x for x in command): raise DirectExecutionError("invalid_request","command must be a non-empty argv array")
  if any(type(x) is not bool for x in (stream_stdin,stream_stdout_stderr,tty)): raise DirectExecutionError("invalid_request","Native stream and tty controls must be booleans")
  if timeoutMs is not None and (type(timeoutMs)is not int or timeoutMs<0): raise DirectExecutionError("invalid_request","timeoutMs must be a native non-negative integer")
  if outputBytesCap is not None and (type(outputBytesCap)is not int or outputBytesCap<0): raise DirectExecutionError("invalid_request","outputBytesCap must be a native non-negative integer")
  if disableTimeout is not None and type(disableTimeout)is not bool or disableOutputCap is not None and type(disableOutputCap)is not bool: raise DirectExecutionError("invalid_request","Native disable flags must be booleans")
  if timeoutMs is not None and disableTimeout: raise DirectExecutionError("invalid_request","timeoutMs cannot be combined with disableTimeout")
  if outputBytesCap is not None and disableOutputCap: raise DirectExecutionError("invalid_request","outputBytesCap cannot be combined with disableOutputCap")
  rel,_=_relative(cwd,workspace["root"])
  request={"workspace_id":workspace["id"],"operation_id":operation_id,"command":command,"cwd":rel,"timeoutMs":timeoutMs,"outputBytesCap":outputBytesCap,"disableTimeout":disableTimeout,"disableOutputCap":disableOutputCap,"stream_stdin":stream_stdin,"stream_stdout_stderr":stream_stdout_stderr,"tty":tty}
  directory=self._dir(workspace["id"],operation_id); directory.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
  with os.fdopen(os.open(directory.parent/".operations.lock",os.O_CREAT|os.O_RDWR,0o600),"a+b") as lock:
   fcntl.flock(lock,fcntl.LOCK_EX); digest=_sha(_json_bytes(request))
   if directory.exists():
    state=_read(directory/"receipt.json")
    if state.get("request_sha256")!=digest: raise DirectExecutionError("idempotency_conflict","operation_id already has different request bytes")
    return dict(_public(state),duplicate_submission=True)
   directory.mkdir(mode=0o700); _atomic_json(directory/"request.json",request); info=workspace["root"].stat()
   socket_dir=Path(tempfile.mkdtemp(prefix="codex-direct-",dir="/tmp"))
   state={"executor_kind":"direct","workspace_id":workspace["id"],"operation_id":operation_id,"request_sha256":digest,"status":"queued","created_at":_now(),"updated_at":_now(),"worker_model":None,"codex_thread_id":None,"workspace_root":str(workspace["root"]),"root_identity":[info.st_dev,info.st_ino],"writer_lock":str(workspace["writer_lock"]),"control_socket":str(socket_dir/"control.sock")}
   _atomic_json(directory/"receipt.json",state)
   codex_bin=self.registry.codex_bin
   if codex_bin is None:
    import shutil
    found=shutil.which("codex")
    if not found: raise DirectExecutionError("unavailable","Codex app-server executable is unavailable")
    codex_bin=Path(found)
   argv=[sys.executable,str(Path(__file__).resolve()),"runner","--operation-dir",str(directory),"--codex-bin",str(codex_bin)]
   with (directory/"launcher.log").open("ab") as log: proc=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,cwd=workspace["root"],start_new_session=True,close_fds=True)
   _update(directory,lambda s:s.update(runner_pid=proc.pid)); return _public(_read(directory/"receipt.json"))
 def get_execution(self,workspace_id,operation_id,output_offset=0,output_limit=None,stdout_offset=None,stderr_offset=None):
  _,directory=self._resolve(workspace_id,operation_id)
  if type(output_offset)is not int or output_offset<0 or output_limit is not None and (type(output_limit)is not int or output_limit<1) or stdout_offset is not None and (type(stdout_offset)is not int or stdout_offset<0) or stderr_offset is not None and (type(stderr_offset)is not int or stderr_offset<0): raise DirectExecutionError("invalid_request","Invalid native output cursor")
  offsets={"stdout":output_offset if stdout_offset is None else stdout_offset,"stderr":output_offset if stderr_offset is None else stderr_offset}
  result=_public(_read(directory/"receipt.json")); result.update(output_encoding="base64",stdout_offset=offsets["stdout"],stderr_offset=offsets["stderr"])
  for stream in ("stdout","stderr"):
   path=directory/(stream+".bin"); data=path.read_bytes()[offsets[stream]:] if path.exists() else b""
   if output_limit is not None:data=data[:output_limit]
   result[stream+"_base64"]=base64.b64encode(data).decode(); result[stream+"_next_offset"]=offsets[stream]+len(data)
  return result
 def write_stdin(self,workspace_id,operation_id,stdin_request_id,data_base64,close_stdin=False):
  _,directory=self._resolve(workspace_id,operation_id)
  if not isinstance(stdin_request_id,str) or not OP_RE.fullmatch(stdin_request_id): raise DirectExecutionError("invalid_request","stdin_request_id must be a safe identifier")
  if not isinstance(data_base64,str) or type(close_stdin)is not bool: raise DirectExecutionError("invalid_request","stdin payload is invalid")
  try: data=base64.b64decode(data_base64,validate=True)
  except ValueError: raise DirectExecutionError("invalid_request","stdin data_base64 is invalid") from None
  request={"stdin_request_id":stdin_request_id,"data_base64":data_base64,"close_stdin":close_stdin}; digest=_sha(_json_bytes(request)); stdin=directory/"stdin"; stdin.mkdir(mode=0o700,exist_ok=True); receipt=stdin/(_key(stdin_request_id)+".json")
  with os.fdopen(os.open(stdin/".lock",os.O_CREAT|os.O_RDWR,0o600),"a+b") as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   if receipt.exists():
    old=_read(receipt)
    if old.get("request_sha256")!=digest: raise DirectExecutionError("idempotency_conflict","stdin_request_id already has different request bytes")
    return dict(old,duplicate_submission=True)
   state=_read(directory/"receipt.json"); sequence=1+max((_read(item).get("sequence",0) for item in stdin.glob("op_*.json")),default=0); saved={"executor_kind":"direct","workspace_id":state["workspace_id"],"operation_id":operation_id,"stdin_request_id":stdin_request_id,"request_sha256":digest,"data_base64":data_base64,"close_stdin":close_stdin,"bytes":len(data),"sequence":sequence,"status":"cancelled" if state.get("status") in TERMINAL else "queued","created_at":_now()}; _atomic_json(receipt,saved)
  response=_control(directory,{"kind":"drain"}) if saved["status"]=="queued" else None
  return response.get("stdin",{}).get(stdin_request_id,saved) if response else saved
 def cancel_execution(self,workspace_id,operation_id):
  _,directory=self._resolve(workspace_id,operation_id); state=_read(directory/"receipt.json")
  if state.get("status") in TERMINAL:return {"status":"already_terminal","operation_id":operation_id}
  _atomic_json(directory/"cancel.json",{"requested_at":_now()}); _update(directory,lambda s:s.update(cancel_requested=True)); _control(directory,{"kind":"cancel"}); return {"status":"cancel_requested","operation_id":operation_id}
 def workspace_diff(self,workspace_id,operation_id):
  workspace,directory=self._resolve(workspace_id,operation_id); before,after=directory/"manifest-before.json",directory/"manifest-after.json"
  if not before.exists() or not after.exists(): raise DirectExecutionError("unavailable","Direct operation has no completed manifest")
  return {"workspace_id":workspace["id"],"operation_id":operation_id,"changed_paths":_manifest_delta(json.loads(before.read_text()),json.loads(after.read_text()))}
 def read_artifact(self,workspace_id,operation_id,path,offset=0,limit=16000):
  workspace,directory=self._resolve(workspace_id,operation_id)
  if type(offset)is not int or offset<0 or type(limit)is not int or limit<1: raise DirectExecutionError("invalid_request","Invalid artifact cursor")
  rel,_=_relative(path,workspace["root"])
  receipt=_read(directory/"receipt.json")
  if rel not in receipt.get("changed_paths",[]): raise DirectExecutionError("scope_violation","Artifact was not created or changed by this operation")
  data=_safe_artifact_read(workspace["root"],rel)
  return {"workspace_id":workspace["id"],"operation_id":operation_id,"path":rel,"offset":offset,"data_base64":base64.b64encode(data[offset:offset+limit]).decode(),"encoding":"base64","next_offset":min(len(data),offset+limit),"sha256":_sha(data)}
 def list_executions(self,workspace_id=None,limit=100):
  if workspace_id is not None and workspace_id not in self.registry.workspaces: self.registry.workspace(workspace_id)
  ids=[workspace_id] if workspace_id is not None else list(self.registry.workspaces); values=[]
  for wid in ids:
   base=self._base(wid)
   if base.exists():
    for path in base.glob("*/receipt.json"):
     try:values.append(_public(_read(path)))
     except DirectExecutionError:pass
  return {"executions":sorted(values,key=lambda x:x.get("created_at",""),reverse=True)[:limit]}
 def active_for_workspace(self,wid):
  # Administration may inspect a disabled workspace's retained receipts; never resolve it for execution.
  return [v for v in self.list_executions(None)["executions"] if v.get("workspace_id")==wid and v.get("status") not in TERMINAL]
 def diagnostics(self):
  import shutil
  codex=self.registry.codex_bin or (Path(shutil.which("codex")) if shutil.which("codex") else None); version=None
  try:
   version=subprocess.check_output([str(codex),"--version"],text=True,stderr=subprocess.DEVNULL,timeout=5).strip() if codex else None
  except (OSError,subprocess.SubprocessError):pass
  compatible=bool(version and re.search(r"(?:^|\s)0\.153\.4(?:\s|$)",version))
  return {"adapter":"codex-app-server-command-exec-only","status":"ready" if compatible else "unchecked","codex_app_server_version":version,"active_executions":sum(len(self.active_for_workspace(w)) for w in self.registry.workspaces)}

class _Control:
 def __init__(self,directory,server,pid,socket_path): self.directory,self.server,self.pid,self.socket_path=directory,server,pid,Path(socket_path); self.sent=threading.Event(); self.stop=threading.Event(); self.lock=threading.Lock(); self.listener=None
 def start(self):
  path=self.socket_path
  try:path.unlink()
  except FileNotFoundError:pass
  self.listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); self.listener.bind(str(path)); os.chmod(path,0o600); self.listener.listen(8); self.listener.settimeout(.25); self.thread=threading.Thread(target=self.serve,daemon=True); self.thread.start()
 def close(self):
  self.stop.set()
  if self.listener:
   try:self.listener.close()
   except OSError:pass
  if hasattr(self,"thread"):self.thread.join(1)
  try:self.socket_path.unlink()
  except FileNotFoundError:pass
  try:self.socket_path.parent.rmdir()
  except OSError:pass
 def serve(self):
  while not self.stop.is_set():
   try:conn,_=self.listener.accept()
   except (OSError,socket.timeout):continue
   with conn:
    try:
     raw=b""
     while not raw.endswith(b"\n") and len(raw)<65536: raw+=conn.recv(4096)
     req=json.loads(raw)
     if req.get("kind")=="cancel":self.cancel()
     elif req.get("kind")!="drain":raise ValueError()
     response={"ok":True,"stdin":self.drain()}
    except Exception:response={"ok":False}
    try:conn.sendall(json.dumps(response,separators=(",",":")).encode()+b"\n")
    except OSError:pass
 def cancel(self):
  if self.sent.is_set():
   try:self.server.terminate(self.pid)
   except AppServerError:pass
 def drain(self):
  sent={}; stdin=self.directory/"stdin"
  if not self.sent.is_set() or (self.directory/"cancel.json").exists() or not stdin.exists():return sent
  with self.lock,os.fdopen(os.open(stdin/".lock",os.O_CREAT|os.O_RDWR,0o600),"a+b") as lock:
   fcntl.flock(lock,fcntl.LOCK_EX)
   for path in sorted(stdin.glob("op_*.json"),key=lambda item: (_read(item).get("sequence",0),item.name)):
    receipt=_read(path)
    if receipt.get("status")!="queued":continue
    try:self.server.write(self.pid,base64.b64decode(receipt["data_base64"],validate=True),bool(receipt["close_stdin"])); receipt.update(status="written",written_at=_now())
    except (AppServerError,ValueError):receipt.update(status="failed",error="Native stdin write failed",completed_at=_now())
    _atomic_json(path,receipt); sent[receipt["stdin_request_id"]]=receipt
  return sent
def _cancel_stdin(directory):
 stdin=directory/"stdin"
 if stdin.exists():
  for path in stdin.glob("op_*.json"):
   value=_read(path)
   if value.get("status")=="queued":value.update(status="cancelled",completed_at=_now());_atomic_json(path,value)
def run_operation(directory,codex_bin):
 request,state=_read(directory/"request.json"),_read(directory/"receipt.json"); root=Path(state["workspace_root"]); before=None; server=None; control=None; final=None
 # This is the delegated-worker lease. It remains held through app-server shutdown and the terminal receipt.
 lock_path=Path(state.get("writer_lock",directory.parents[1]/"workspace.lock"));lock_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
 lock=os.fdopen(os.open(lock_path,os.O_CREAT|os.O_RDWR,0o600),"a+b"); fcntl.flock(lock,fcntl.LOCK_EX)
 try:
  info=root.stat()
  if [info.st_dev,info.st_ino]!=state["root_identity"]:raise OSError("workspace root changed")
  if (directory/"cancel.json").exists():
   _cancel_stdin(directory); final={"status":"cancelled","cancel_requested":True,"completed_at":_now()}; return
  _update(directory,lambda s:s.update(status="running",started_at=_now()));before=_workspace_manifest(root);_atomic_json(directory/"manifest-before.json",before)
  server=CodexAppServer(codex_bin,root,directory/"private",[]);server.start(); control=_Control(directory,server,"direct_"+_sha(state["operation_id"].encode())[:20],state["control_socket"]);control.start()
  def on_sent():
   control.sent.set();threading.Thread(target=control.drain,daemon=True).start()
   if (directory/"cancel.json").exists():threading.Thread(target=control.cancel,daemon=True).start()
  def output(stream,data,capped):
   if stream in {"stdout","stderr"}:
    with (directory/(stream+".bin")).open("ab",buffering=0) as out:out.write(data)
   if capped:_update(directory,lambda s:s.update(output_capped=True))
  native=server.exec(process_id=control.pid,command=request["command"],cwd=str(root/request["cwd"]),timeout_ms=request["timeoutMs"],output_cap=request["outputBytesCap"],disable_timeout=request.get("disableTimeout"),disable_output_cap=request.get("disableOutputCap"),stream_stdin=request["stream_stdin"],stream_stdout_stderr=request["stream_stdout_stderr"],tty=request["tty"],on_output=output,on_request_sent=on_sent,sandbox_policy={"type":"dangerFullAccess"})
  # Buffered native execution returns final stdout/stderr instead of outputDelta notifications.
  for stream,key in (("stdout","stdout"),("stderr","stderr")):
   value=native.get(key)
   if isinstance(value,str):
    with (directory/(stream+".bin")).open("ab") as out:out.write(value.encode())
  after=_workspace_manifest(root);_atomic_json(directory/"manifest-after.json",after); cancelled=(directory/"cancel.json").exists()
  if cancelled:_cancel_stdin(directory)
  code=native.get("exitCode"); good=type(code)is int and not isinstance(code,bool) and code==0
  final={"status":"cancelled" if cancelled else "succeeded" if good else "failed","cancel_requested":cancelled,"exit_code":code if type(code)is int and not isinstance(code,bool) else None,"native_result":{k:native.get(k) for k in ("exitCode","stdout","stderr","stdoutTruncated","stderrTruncated") if k in native},"changed_paths":_manifest_delta(before,after),"unexpected_changed_paths":[],"completed_at":_now()}
 except Exception:
  try:
   after=_workspace_manifest(root) if before is not None else None
   if after is not None:_atomic_json(directory/"manifest-after.json",after)
   cancelled=(directory/"cancel.json").exists()
   if cancelled:_cancel_stdin(directory)
   final={"status":"cancelled" if cancelled else "failed","cancel_requested":cancelled,"error":"Direct command failed","changed_paths":_manifest_delta(before,after) if before is not None and after is not None else [],"unexpected_changed_paths":[],"completed_at":_now()}
  except Exception:final={"status":"failed","error":"Direct command failed","completed_at":_now()}
 finally:
  # Do not allow another writer or a terminal Admin receipt before owned native processes are closed.
  if control:control.close()
  if server:
   try:server.stop()
   except Exception:pass
  if final:_update(directory,lambda receipt:receipt.update(**final))
  fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
def main():
 p=argparse.ArgumentParser();p.add_argument("command",choices=["runner"]);p.add_argument("--operation-dir",type=Path,required=True);p.add_argument("--codex-bin",type=Path,required=True);a=p.parse_args();run_operation(a.operation_dir,a.codex_bin)
if __name__=="__main__":main()

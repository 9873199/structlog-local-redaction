"""Exercise local-name redaction before any representation, including real renderer output."""
import dataclasses,json,sys
from pathlib import Path
import pytest
import structlog
from structlog import tracebacks
from structlog.processors import ExceptionRenderer,JSONRenderer

class SecretObject:
 def __init__(self):self.calls=[]
 def __repr__(self):self.calls.append('repr');raise RuntimeError('representation forbidden')
 def __str__(self):self.calls.append('str');raise RuntimeError('string forbidden')
 def __rich_repr__(self):self.calls.append('rich');raise RuntimeError('rich protocol forbidden')

def capture(secret_value):
 try:
  password=secret_value
  Access_TOKEN=secret_value
  ordinary='visible business value'
  raise ValueError('business operation failed')
 except ValueError:return sys.exc_info()

def all_locals(value):
 result=[]
 if isinstance(value,dict):
  if value.get('locals') is not None:result.append(value['locals'])
  for v in value.values():result.extend(all_locals(v))
 elif isinstance(value,list):
  for v in value:result.extend(all_locals(v))
 return result

@pytest.mark.parametrize('rich_enabled',[True,False])
@pytest.mark.parametrize('entry',['transformer','extract','json'])
def test_redaction_precedes_representation(monkeypatch,rich_enabled,entry):
 if not rich_enabled:monkeypatch.setattr(tracebacks,'rich',None)
 secret=SecretObject();info=capture(secret)
 patterns=['password','*token*','secret*']
 transformer=tracebacks.ExceptionDictTransformer(show_locals=True,redact_locals=patterns)
 if entry=='transformer':result=transformer(info)
 elif entry=='extract':result=dataclasses.asdict(tracebacks.extract(*info,show_locals=True,redact_locals=patterns))
 else:
  renderer=ExceptionRenderer(exception_formatter=transformer)
  output=renderer(None,'error',{'event':'request failed','exc_info':info})
  result=json.loads(JSONRenderer()(None,'error',output))
 frames=all_locals(result)
 assert any(f.get('password')=='<redacted>' and f.get('Access_TOKEN')=='<redacted>' for f in frames)
 assert any('visible business value' in f.get('ordinary','') for f in frames)
 assert secret.calls==[]

def test_configuration_is_frozen_copy():
 patterns=['password','*token*','secret*']
 transformer=tracebacks.ExceptionDictTransformer(redact_locals=patterns)
 patterns.clear();patterns.append('*')
 secret=SecretObject();frames=all_locals(transformer(capture(secret)))
 assert any(f.get('password')=='<redacted>' for f in frames)
 assert any(f.get('ordinary')!='<redacted>' for f in frames if 'ordinary' in f)
 assert secret.calls==[]

@pytest.mark.parametrize('show_locals',[True,False])
def test_suppressed_or_hidden_frames_do_not_touch_unmatched_values(show_locals):
 secret=SecretObject()
 def fail():
  ordinary=secret
  raise ValueError('normal message')
 def caught():
  try:fail()
  except ValueError:return sys.exc_info()
 transformer=tracebacks.ExceptionDictTransformer(show_locals=show_locals,suppress=[str(Path(__file__).resolve())],redact_locals=['password'])
 transformer(caught())
 assert secret.calls==[]

@pytest.mark.parametrize('chain',['cause','context','group'])
def test_exception_branches_share_redaction_policy(chain):
 secret=SecretObject();info=capture(secret);first=info[1]
 try:
  secret_token=secret
  if chain=='group':raise ExceptionGroup('branch errors',[first,ExceptionGroup('nested',[capture(secret)[1]])])
  try:raise first
  except ValueError:
   password=secret
   if chain=='cause':raise RuntimeError('wrapper') from first
   raise RuntimeError('wrapper')
 except BaseException:combined=sys.exc_info()
 transformer=tracebacks.ExceptionDictTransformer(redact_locals=['password','*token*','secret*'])
 frames=all_locals(transformer(combined))
 assert any(f.get('password')=='<redacted>' for f in frames)
 assert secret.calls==[]

def test_no_configuration_keeps_ordinary_locals():
 def fail():
  ordinary='previous behavior'
  raise ValueError('normal')
 try:fail()
 except ValueError:info=sys.exc_info()
 frames=all_locals(tracebacks.ExceptionDictTransformer()(info))
 assert any('previous behavior' in f.get('ordinary','') for f in frames)

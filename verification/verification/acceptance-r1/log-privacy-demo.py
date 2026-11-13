import json,sys
from unittest.mock import patch
from structlog import tracebacks
from structlog.processors import ExceptionRenderer,JSONRenderer
from test_locals_contract import SecretObject,capture,all_locals
rich_module=tracebacks.rich
for enabled in (False,True):
 password=SecretObject();patterns=['password','*token*','secret*'];transformer=tracebacks.ExceptionDictTransformer(redact_locals=patterns);patterns.clear()
 with patch.object(tracebacks,'rich',rich_module if enabled else None):
  payload=ExceptionRenderer(exception_formatter=transformer)(None,'error',{'event':'request failed','exc_info':capture(password)})
  result=json.loads(JSONRenderer()(None,'error',payload))
  frames=all_locals(result);selected=next(x for x in frames if 'ordinary' in x)
 print('Rich enabled:',enabled)
 print('Selected local values:',{k:selected[k] for k in ('password','Access_TOKEN','ordinary')})
 print('Sensitive object representation calls:',password.calls)
 assert password.calls==[] and selected['password']=='<redacted>' and selected['Access_TOKEN']=='<redacted>'
 print('Caller pattern list changed after construction; policy retained:',transformer.redact_locals)
print('Only selected local names are protected; exception text keeps its normal meaning.')

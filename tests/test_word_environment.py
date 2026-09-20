import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from skill_toolbox.word_com import check_registered_word, validate_version, close_word


def test_wps_registration_rejected_before_start(monkeypatch):
    class Key:
        def __init__(self,path): self.path=path
        def __enter__(self): return self
        def __exit__(self,*args): pass
    monkeypatch.setitem(sys.modules,'winreg',SimpleNamespace(HKEY_CLASSES_ROOT=0,OpenKey=lambda _,path:Key(path),
        QueryValueEx=lambda key,_: ('{word}' if key.path.endswith('CLSID') else '"C:\\Kingsoft\\wps.exe" /Automation',0)))
    with pytest.raises(RuntimeError,match='不是 Microsoft Word'):
        check_registered_word()


def test_old_word_version_rejected():
    with pytest.raises(RuntimeError,match='WORD_UNAVAILABLE'):
        validate_version('12.0')
    validate_version('16.0')


def test_cleanup_preserves_original_failure_and_uninitializes(monkeypatch):
    com=SimpleNamespace(CoUninitialize=Mock())
    monkeypatch.setitem(sys.modules,'pythoncom',com)
    word=SimpleNamespace(Quit=Mock(side_effect=AttributeError('Word.Application.Quit')))
    doc=SimpleNamespace(Close=Mock(side_effect=RuntimeError('close failed')))
    with pytest.raises(ValueError,match='original shape missing') as error:
        try:
            raise ValueError('original shape missing')
        finally:
            close_word(word,doc)
    assert len(error.value.__notes__)==2
    com.CoUninitialize.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('code,expected_calls',[('MEASURE_FAILED',3),('WORD_UNAVAILABLE',1)])
async def test_runtime_stops_repeated_environment_errors(tmp_path,monkeypatch,code,expected_calls):
    from skill_toolbox.runtime import AgentRuntime,TaskRequest
    from skill_toolbox.models import AssistantTurn,ToolCall,ToolResult
    from skill_toolbox.providers.mock import ScriptedProvider
    provider=ScriptedProvider([AssistantTurn(tool_calls=[ToolCall(id=str(i),name='resume_prepare',arguments={})]) for i in range(24)])
    count=0
    async def execute(self,calls,*args):
        nonlocal count
        count+=1
        return [ToolResult(tool_call_id=calls[0].id,name='resume_prepare',success=False,
                           content=json.dumps({'code':code,'message':'Word COM failed','retryable':True}))]
    monkeypatch.setattr(AgentRuntime,'_execute_calls',execute)
    result=await AgentRuntime(provider,lambda e:None).run(TaskRequest('resume_pro','test',tmp_path/'out',
                                      template_id='t109',tool_mode='domain',capabilities={'vision':True,'tool_calling':True}))
    assert result.status=='failed'
    assert code in result.error
    assert count==expected_calls

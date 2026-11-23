"""A durable compile/patch/retry through the real PS4 compiler and strict app judge."""
import re

from decompnet.app import compile_operations, compile_tools
from decompnet.forks import csource

from .test_ps4_workflows import ROOT, ps4_demo  # noqa: F401 fixture uses a disposable home and pinned tools


def test_completed_real_compile_retry_returns_the_original_operation_after_a_match(ps4_demo):  # noqa: F811 fixture
    net, slug, _ = ps4_demo
    agent, _ = net.local_agent('durable-compile-check')
    correct = csource.get((ROOT / 'projects/demo-ps4/extras.c').read_text(), 'demo_sum')
    net.claim(agent, slug, 'demo_sum')
    initial = dict(function='demo_sum', request_id='first-definition', revision='', action='definition',
                   source=correct.replace('a + b', 'a - b'))
    try:
        compile_operations.start(net, agent, slug, **initial)
        service = compile_operations.manager(net)
        service.queue.join()
        brief = compile_tools.brief(net, agent, slug, 'demo_sum')
        current = re.search(r'revision: ([0-9a-f]{64})', brief)[1]
        args = dict(function='demo_sum', request_id='exact-patch', revision=current,
                    edits=[{'old': 'a - b', 'new': 'a + b'}])
        started = compile_operations.start(net, agent, slug, **args)
        service.queue.join()
        assert net._function('demo_sum', slug)['status'] == 'matched'
        assert net.store.one('SELECT COUNT(*) n FROM claims WHERE agent_id=?', agent.id)['n'] == 0
        again = compile_operations.start(net, agent, slug, **args)
        assert 'completed' in again and started.split(':', 1)[0] in again
        assert net.store.one("SELECT COUNT(*) n FROM compile_operations WHERE request_key='exact-patch'")['n'] == 1
    finally:
        if getattr(net, '_compile_operations', None):
            net._compile_operations.close()

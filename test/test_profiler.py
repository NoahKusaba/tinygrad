import unittest, struct, contextlib, statistics
from tinygrad import Device, Tensor, dtypes, TinyJit
from tinygrad.helpers import CI, getenv, Context
from tinygrad.device import Buffer, BufferSpec, Compiled, ProfileRangeEvent, ProfileDeviceEvent, ProfileGraphEvent
from tinygrad.runtime.support.hcq import HCQCompiled
from tinygrad.engine.realize import get_runner

MOCKGPU = getenv("MOCKGPU")

@contextlib.contextmanager
def helper_collect_profile(*devs):
  for dev in devs: dev.synchronize()
  Compiled.profile_events = [x for x in Compiled.profile_events if isinstance(x, ProfileDeviceEvent) and x.device.startswith("METAL")]

  profile_list = []
  with Context(PROFILE=1):
    yield profile_list
    for dev in devs: dev.synchronize()
    for dev in devs: dev._at_profile_finalize()
    for x in Compiled.profile_events: profile_list.append(x)

def helper_profile_filter_device(profile, device:str):
  assert any(getattr(x, "device", None) == device and isinstance(x, ProfileDeviceEvent) for x in profile), f"device {device} is not registred"
  dev_events = [x for x in profile if getattr(x, "device", None) == device and isinstance(x, ProfileDeviceEvent)]
  assert len(dev_events) == 1, "only one device registration event is expected"
  return [x for x in profile if getattr(x, "device", None) == device], dev_events[0]

@unittest.skipUnless(issubclass(type(Device[Device.DEFAULT]), HCQCompiled) or Device.DEFAULT in {"METAL"}, "HCQ device required to run")
class TestProfiler(unittest.TestCase):
  @classmethod
  def setUpClass(self):
    TestProfiler.d0 = Device[Device.DEFAULT]

    TestProfiler.a = Tensor([0.,1.], device=Device.DEFAULT).realize()
    TestProfiler.b = self.a + 1
    si = self.b.schedule()[-1]

    TestProfiler.runner = get_runner(TestProfiler.d0.device, si.ast)
    TestProfiler.b.uop.buffer.allocate()

  def test_profile_kernel_run(self):
    runner_name = TestProfiler.runner._prg.name
    with helper_collect_profile(TestProfiler.d0) as profile:
      TestProfiler.runner([TestProfiler.b.uop.buffer, TestProfiler.a.uop.buffer], var_vals={})

    profile, _ = helper_profile_filter_device(profile, TestProfiler.d0.device)
    kernel_runs = [x for x in profile if isinstance(x, ProfileRangeEvent)]
    assert len(kernel_runs) == 1, "one kernel run is expected"
    assert kernel_runs[0].name == runner_name, "kernel name is not correct"
    assert not kernel_runs[0].is_copy, "kernel should not be copy"

  def test_profile_copyin(self):
    buf1 = Buffer(Device.DEFAULT, 2, dtypes.float, options=BufferSpec(nolru=True)).ensure_allocated()

    with helper_collect_profile(TestProfiler.d0) as profile:
      buf1.copyin(memoryview(bytearray(struct.pack("ff", 0, 1))))

    profile, _ = helper_profile_filter_device(profile, TestProfiler.d0.device)
    kernel_runs = [x for x in profile if isinstance(x, ProfileRangeEvent)]
    assert len(kernel_runs) == 1, "one kernel run is expected"
    assert kernel_runs[0].is_copy, "kernel should be copy"

  def test_profile_multiops(self):
    runner_name = TestProfiler.runner._prg.name
    buf1 = Buffer(Device.DEFAULT, 2, dtypes.float, options=BufferSpec(nolru=True)).ensure_allocated()

    with helper_collect_profile(TestProfiler.d0) as profile:
      buf1.copyin(memoryview(bytearray(struct.pack("ff", 0, 1))))
      TestProfiler.runner([buf1, TestProfiler.a.uop.buffer], var_vals={})
      buf1.copyout(memoryview(bytearray(buf1.nbytes)))

    profile, _ = helper_profile_filter_device(profile, TestProfiler.d0.device)
    evs = [x for x in profile if isinstance(x, ProfileRangeEvent)]

    assert len(evs) == 3, "3 kernel runs are expected"
    assert evs[0].is_copy, "kernel should be copy"
    assert evs[1].name == runner_name, "kernel name is not correct"
    assert not evs[1].is_copy, "kernel should not be copy"
    assert evs[2].is_copy, "kernel should be copy"

    for i in range(1, 3):
      assert evs[i].st > evs[i-1].en, "timestamp not aranged"

  def test_profile_multidev(self):
    d1 = Device[f"{Device.DEFAULT}:1"]
    buf1 = Buffer(Device.DEFAULT, 2, dtypes.float, options=BufferSpec(nolru=True)).ensure_allocated()
    buf2 = Buffer(f"{Device.DEFAULT}:1", 2, dtypes.float, options=BufferSpec(nolru=True)).ensure_allocated()

    with helper_collect_profile(TestProfiler.d0, d1) as profile:
      buf1.copyin(memoryview(bytearray(struct.pack("ff", 0, 1))))
      buf2.copyin(memoryview(bytearray(struct.pack("ff", 0, 1))))

    profile0, _ = helper_profile_filter_device(profile, TestProfiler.d0.device)
    profile1, _ = helper_profile_filter_device(profile, d1.device)

    for p in [profile0, profile1]:
      evs = [x for x in p if isinstance(x, ProfileRangeEvent)]
      assert len(evs) == 1, "one kernel runs are expected"
      assert evs[0].is_copy, "kernel should be copy"

  def test_profile_multidev_transfer(self):
    d1 = Device[f"{Device.DEFAULT}:1"]

    buf1 = Tensor.randn(10, 10, device=f"{Device.DEFAULT}:0").realize()
    with helper_collect_profile(TestProfiler.d0, d1) as profile:
      buf1.to(f"{Device.DEFAULT}:1").realize()

    profile0, _ = helper_profile_filter_device(profile, TestProfiler.d0.device)
    kernel_runs = [x for x in profile0 if isinstance(x, ProfileRangeEvent)]
    assert len(kernel_runs) == 1, "one kernel run is expected"
    assert kernel_runs[0].is_copy, "kernel should be copy"

  @unittest.skipIf(Device.DEFAULT in "METAL" or (MOCKGPU and Device.DEFAULT == "AMD"), "AMD mockgpu does not support queue wait interrupts")
  def test_profile_graph(self):
    d1 = Device[f"{Device.DEFAULT}:1"]

    def f(a):
      x = (a + 1).realize()
      return x, x.to(d1.device).realize()

    a = Tensor.randn(10, 10, device=TestProfiler.d0.device).realize()
    with helper_collect_profile(TestProfiler.d0, d1) as profile:
      jf = TinyJit(f)
      for _ in range(3): jf(a)
      del jf

    graph_evs = [x for x in profile if isinstance(x, ProfileGraphEvent)]

    _, _ = helper_profile_filter_device(profile, TestProfiler.d0.device)
    _, _ = helper_profile_filter_device(profile, d1.device)

    assert len(graph_evs) == 1, "one graph event is expected"
    assert len(graph_evs[0].ents) == 2, "two entities are expected"

  @unittest.skipIf(CI or not issubclass(type(Device[Device.DEFAULT]), HCQCompiled), "skip CI")
  def test_dev_jitter_matrix(self):
    dev_cnt = 6
    devs = [Device[f"{Device.DEFAULT}:{i}"] for i in range(dev_cnt)]
    for dev in devs: dev.synchronize()
    for dev in devs: dev._at_profile_finalize()

    def _sync_d2d(d1:HCQCompiled, d2:HCQCompiled):
      d1.hw_compute_queue_t().signal(d1.timeline_signal, d1.timeline_value).wait(d2.timeline_signal, d2.timeline_value) \
                             .timestamp(d1.timeline_signal).signal(d1.timeline_signal, d1.timeline_value+1).submit(d1)
      d2.hw_compute_queue_t().signal(d2.timeline_signal, d2.timeline_value).wait(d1.timeline_signal, d1.timeline_value) \
                             .timestamp(d2.timeline_signal).signal(d2.timeline_signal, d2.timeline_value+1).submit(d2)
      d1.timeline_value += 2
      d2.timeline_value += 2
      d1.timeline_signal.wait(d1.timeline_value - 1)
      d2.timeline_signal.wait(d2.timeline_value - 1)
      return d2.timeline_signal.timestamp - d1.timeline_signal.timestamp

    # then test it by timing the GPU to GPU times
    jitter_matrix = [[float('nan')] * len(devs) for _ in range(len(devs))]
    pairs = [(p1, p2) for p1 in enumerate(devs) for p2 in enumerate(devs) if p1 != p2]
    for (i1, d1), (i2, d2) in pairs:
      cpu_diff = d1.gpu2cpu_compute_time_diff - d2.gpu2cpu_compute_time_diff
      jitter_matrix[i1][i2] = statistics.median(_sync_d2d(d1, d2) - _sync_d2d(d2, d1) for _ in range(20)) / 2 - cpu_diff
      assert abs(jitter_matrix[i1][i2]) < 0.5, "jitter should be less than 0.5ms"
    print("pairwise clock jitter matrix (us):\n" + '\n'.join([''.join([f'{float(item):8.3f}' for item in row]) for row in jitter_matrix]))

if __name__ == "__main__":
  unittest.main()
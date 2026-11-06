from studio.rendering import gpu_preference as pref


def test_linux_pci_choice_configures_mesa_and_clears_nvidia(monkeypatch):
    monkeypatch.setattr(pref.sys, 'platform', 'linux')
    monkeypatch.setattr(pref, 'linux_graphics_devices', lambda: [('0000:01:00.0', '0x1002')])
    monkeypatch.setenv('__NV_PRIME_RENDER_OFFLOAD', '1')
    monkeypatch.setenv('__GLX_VENDOR_LIBRARY_NAME', 'nvidia')
    assert pref.apply_linux_device_preference('0000:01:00.0|0x1002')
    assert pref.os.environ['DRI_PRIME'] == 'pci-0000_01_00_0'
    assert '__NV_PRIME_RENDER_OFFLOAD' not in pref.os.environ
    assert '__GLX_VENDOR_LIBRARY_NAME' not in pref.os.environ


def test_linux_stale_device_does_not_change_environment(monkeypatch):
    monkeypatch.setattr(pref.sys, 'platform', 'linux')
    monkeypatch.setattr(pref, 'linux_graphics_devices', lambda: [])
    monkeypatch.setenv('DRI_PRIME', 'existing')
    assert not pref.apply_linux_device_preference('0000:01:00.0|0x1002')
    assert pref.os.environ['DRI_PRIME'] == 'existing'


def test_linux_nvidia_choice_requests_prime(monkeypatch):
    monkeypatch.setattr(pref.sys, 'platform', 'linux')
    monkeypatch.setattr(pref, 'linux_graphics_devices', lambda: [('0000:02:00.0', '0x10de')])
    monkeypatch.setenv('DRI_PRIME', 'old')
    assert pref.apply_linux_device_preference('0000:02:00.0|0x10de')
    assert pref.os.environ['__GLX_VENDOR_LIBRARY_NAME'] == 'nvidia'
    assert pref.os.environ['__NV_PRIME_RENDER_OFFLOAD'] == '1'
    assert 'DRI_PRIME' not in pref.os.environ

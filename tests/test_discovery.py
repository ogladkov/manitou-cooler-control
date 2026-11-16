from lianli_lcd.discovery import discover


def test_only_lcd_endpoints_and_stable_identity(tmp_path):
    usb = tmp_path / "usb"
    hid = tmp_path / "hid"
    usb.mkdir()
    hid.mkdir()
    for topology, vid, pid in [("3-1.2", "04fc", "7393"), ("3-1.3", "1cbe", "0006"),
                                ("3-1.4", "0416", "7372"), ("3-1.5", "0416", "8041")]:
        device = usb / topology
        device.mkdir()
        for name, value in [("idVendor", vid), ("idProduct", pid), ("busnum", "3"),
                            ("devnum", "12"), ("serial", "nonunique")]:
            (device / name).write_text(value)
    hid_device = usb / "3-1.2" / "3-1.2:1.0" / "hiddevice"
    hid_device.mkdir(parents=True)
    (hid / "hidraw5").mkdir()
    (hid / "hidraw5" / "device").symlink_to(hid_device, target_is_directory=True)
    before = discover(usb, hid)
    assert len(before) == 2
    assert before[0].hidraw == "/dev/hidraw5"
    (usb / "3-1.2" / "devnum").write_text("25")
    after = discover(usb, hid)
    assert after[0].id == before[0].id and after[0].address == 25

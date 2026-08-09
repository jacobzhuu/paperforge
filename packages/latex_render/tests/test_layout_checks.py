from latex_render.compile import layout_checks


def test_layout_checks_flag_only_material_overfull_boxes():
    clean = layout_checks("warning: Overfull \\hbox (3.2pt too wide)")
    bad = layout_checks("warning: Overfull \\hbox (33.45613pt too wide)")
    assert clean["passed"] is True
    assert bad["passed"] is False
    assert bad["overfull_over_threshold"] == 1

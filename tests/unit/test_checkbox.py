"""The checkbox square in a picture of a check (openajar.node.checkbox)."""

from __future__ import annotations

from PIL import Image, ImageDraw

from openajar.node.checkbox import find


def widget(size=(300, 65), at=(9, 21), side=24, bg=(250, 250, 250), ring=(102, 102, 102), text=True):
    image = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, size[0] - 1, size[1] - 1], outline=(224, 224, 224))  # the widget's own border
    x, y = at
    if side:
        draw.rectangle([x, y, x + side - 1, y + side - 1], outline=ring, width=2)
    if text:
        draw.text((x + side + 10, y + 5), "Verify you are human", fill=(30, 30, 30))
        draw.text((size[0] - 80, 20), "CLOUDFLARE", fill=(240, 130, 30))
    return image


def test_the_square_is_found_where_it_is():
    assert find(widget()) == (21.0, 33.0, 24)


def test_a_widget_centred_in_a_wider_box_and_a_dark_theme():
    assert find(widget(size=(480, 69), at=(99, 22))) == (111.0, 34.0, 24)
    assert find(widget(bg=(34, 34, 34), ring=(220, 220, 220))) == (21.0, 33.0, 24)


def test_recaptchas_light_grey_box_counts():
    assert find(widget(size=(304, 78), at=(12, 26), side=28, bg=(249, 249, 249), ring=(193, 193, 193))) \
        == (26.0, 40.0, 28)


def test_no_square_no_answer():
    assert find(widget(side=0, text=True).crop((40, 0, 300, 65))) is None
    assert find(Image.new("RGB", (200, 60), (255, 255, 255))) is None

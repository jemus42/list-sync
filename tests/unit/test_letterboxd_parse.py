"""Letterboxd HTML parsing helpers."""

from list_sync.providers.letterboxd import _next_page_url, _parse_list_page

_GRID = '''
<li class="griditem">
 <div class="react-component" data-component-class="LazyPoster" data-item-name="Ring (1998)"
      data-item-slug="ring-1998" data-item-link="/film/ring-1998/"></div>
</li>
<li class="griditem">
 <div class="react-component" data-item-slug="wall-e" data-item-name="WALL&#183;E &amp; Friends (2008)"></div>
</li>
<li class="griditem">
 <div class="react-component" data-item-slug="untitled" data-item-name="Untitled Project"></div>
</li>
<div class="pagination">
 <div class="paginate-nextprev"><a class="next" href="/jemus42/watchlist/page/2/">Older</a></div>
</div>
'''


def test_parse_list_page():
    items = _parse_list_page(_GRID)
    assert len(items) == 3, items

    assert items[0] == {
        "title": "Ring", "media_type": "movie", "year": 1998, "slug": "ring-1998"
    }, items[0]

    # entities decoded, attribute order reversed
    assert items[1]["title"] == "WALL·E & Friends", items[1]
    assert items[1]["year"] == 2008 and items[1]["slug"] == "wall-e"

    # no year in the name -> year stays None rather than mangling the title
    assert items[2]["title"] == "Untitled Project" and items[2]["year"] is None, items[2]

    assert _next_page_url(_GRID) == "https://letterboxd.com/jemus42/watchlist/page/2/"
    assert _next_page_url("<div class='pagination'></div>") is None

    # a "next" inside another class name must not match
    assert _next_page_url('<a class="nextprev-thing" href="/x/">x</a>') is None

    # a year inside the title must not be mistaken for the release year
    yr = _parse_list_page(
        '<div class="react-component" data-item-slug="blade-runner-2049"'
        ' data-item-name="Blade Runner 2049 (2017)"></div>'
    )
    assert yr[0]["title"] == "Blade Runner 2049" and yr[0]["year"] == 2017, yr[0]

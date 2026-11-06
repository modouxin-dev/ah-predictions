import csv
from datetime import datetime, timezone

import pytest

from ah import football_data as fd
from ah.enrich import enrich
from ah.settle import autofill_results

M = {"id": 555, "utcDate": "2026-11-04T20:00:00Z", "status": "FINISHED",
     "homeTeam": {"id": 503, "name": "FC Porto", "shortName": "Porto"},
     "awayTeam": {"id": 113, "name": "SSC Napoli", "shortName": "Napoli"},
     "score": {"duration": "REGULAR", "fullTime": {"home": 2, "away": 1}}}
OLD = [dict(M, id=i, utcDate=f"2026-10-{10+i}T19:00:00Z",
            score={"duration": "REGULAR", "fullTime": {"home": h, "away": a}})
       for i, (h, a) in enumerate([(2, 0), (1, 1), (0, 1), (3, 0), (2, 2)])]
STAND = {"standings": [{"type": "TOTAL", "group": None, "table": [
    {"position": 5, "team": {"id": 503}, "points": 7, "playedGames": 3, "goalDifference": 3},
    {"position": 12, "team": {"id": 113}, "points": 4, "playedGames": 3, "goalDifference": 0}]}]}


class R:
    def __init__(self, data, status=200, headers=None):
        self.d, self.status_code, self.headers = data, status, headers or {"X-Requests-Available-Minute": "9"}

    def json(self):
        return self.d


class S:
    def __init__(self, routes, seq=None):
        self.routes, self.seq, self.calls = routes, list(seq or []), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self.seq:
            return self.seq.pop(0)
        for k, v in self.routes.items():
            if k in url:
                return R(v)
        return R({}, 404)


def client(tmp_path, routes=None, seq=None, sleeps=None):
    routes = routes or {"standings": STAND, "teams/503/matches": {"matches": OLD},
                        "teams/113/matches": {"matches": OLD}, "head2head": {"matches": OLD[:3]},
                        "/matches": {"matches": [M]}}
    return fd.Client(key="KEY", session=S(routes, seq), cache_dir=tmp_path,
                     sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_header_and_429_backoff(tmp_path):
    sl = []
    c = client(tmp_path, seq=[R({}, 429, {"X-RequestCounter-Reset": "7"}), R({"matches": [M]})], sleeps=sl)
    assert c.matches("2026-11-04")[0]["id"] == 555
    assert sl == [7.0] and c.s.calls[0][2] == {"X-Auth-Token": "KEY"}
    assert "KEY" not in repr(c)


def test_throttle_when_exhausted(tmp_path):
    sl = []
    c = client(tmp_path, seq=[R({"matches": []}, headers={"X-Requests-Available-Minute": "0",
                                                          "X-RequestCounter-Reset": "30"}), R({"matches": []})], sleeps=sl)
    c.matches("2026-11-01"); c.matches("2026-11-02")
    assert sl == [31.0]


def test_error_hides_key(tmp_path):
    c = client(tmp_path, seq=[R({}, 403)])
    with pytest.raises(fd.FootballDataError) as e:
        c.standings("CL")
    assert "KEY" not in str(e.value)


def test_pure_helpers():
    assert fd.fulltime(M) == (2, 1)
    et = dict(M, score={"duration": "PENALTY_SHOOTOUT", "fullTime": {"home": 4, "away": 3},
                        "regularTime": {"home": 1, "away": 1}})
    assert fd.fulltime(et) == (1, 1)
    assert fd.fulltime(dict(M, status="TIMED")) is None
    assert fd.form(OLD, 503) == "DWLDW"
    assert fd.table_row(STAND, 113)["position"] == 12
    assert fd.find_match([M], "波尔图 Porto", "那不勒斯 Napoli", "2026-11-04T20:10:00Z", aliases={})["id"] == 555
    assert fd.find_match([M], "Benfica", "Napoli", "2026-11-04T20:00:00Z", aliases={}) is None


def test_fundamentals_and_enrich(tmp_path):
    c = client(tmp_path)
    f = fd.fundamentals(c, "CL", "Porto", "Napoli", "2026-11-04T20:00:00Z", "home", aliases={})
    assert f["fd_match_id"] == 555 and f["standings"]["home"]["position"] == 5 and f["h2h"]["played"] == 3

    class AF:
        cache_dir = tmp_path

        def h2h(self, *a, **k):
            return []

    fx = {"api_fixture_id": 1, "home_id": 1, "away_id": 2, "home": "Porto", "away": "Napoli",
          "kickoff_utc": "2026-11-04T20:00:00Z"}
    e = enrich(fx, client=AF(), injuries_covered=False, want_odds=False, fd_code="CL", fd_client=c)
    assert e["football_data"]["fd_match_id"] == 555
    assert e["form"]["note"].startswith("football-data")


def _csv(p):
    p.write_text("date,match_id,league,home,away,kickoff_utc,pick_side,result,settled\n"
                 "2026-11-04,t1,UEFA Champions League,Porto,Napoli,2026-11-04T20:00:00Z,home,,\n", encoding="utf-8")


class AFC:
    def __init__(self, hg, ag):
        self.hg, self.ag = hg, ag

    def fixtures_by_date(self, d):
        return [{"fixture": {"id": 9, "date": "2026-11-04T20:00:00+00:00"},
                 "teams": {"home": {"name": "FC Porto"}, "away": {"name": "Napoli"}}}]

    def fixture_result(self, fid):
        return {"finished": True, "home_goals": self.hg, "away_goals": self.ag, "home": "FC Porto", "away": "Napoli", "status": "FT"}


NOW = datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc)


def test_settle_cross_verify(tmp_path):
    p = tmp_path / "p.csv"; _csv(p)
    log = autofill_results(p, client=AFC(2, 1), now=NOW, fd_client=client(tmp_path / "c1"))
    assert log[0]["action"] == "filled" and "一致" in log[0]["detail"]
    _csv(p)
    log = autofill_results(p, client=AFC(1, 1), now=NOW, fd_client=client(tmp_path / "c2"))
    assert log[0]["action"] == "mismatch"
    assert list(csv.DictReader(p.open()))[0]["result"] == ""


def test_settle_falls_back_when_api_football_disabled(tmp_path):
    from ah.api_football import ApiFootballError

    class Disabled:
        def fixtures_by_date(self, _date):
            raise ApiFootballError('disabled')

    p = tmp_path / 'p.csv'
    _csv(p)
    log = autofill_results(p, client=Disabled(), now=NOW, fd_client=client(tmp_path / 'cache'))
    assert log[0]['action'] == 'filled'
    assert list(csv.DictReader(p.open()))[0]['result'] == '2-1'

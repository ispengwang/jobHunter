"""统一 schema 与薪资解析。

三个平台字段各不相同，这里定义 pipeline 内部唯一的数据结构。
薪资解析原则：解析不出来就留 None,不猜。宁可缺字段,不要错字段。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional


FIELDS = [
    "id", "source", "title", "company", "location",
    "salary_min", "salary_max", "salary_raw", "work_type",
    "posted_date", "url", "description", "duplicate_urls",
]


@dataclass
class Job:
    source: str                      # linkedin | indeed | seek
    title: str
    company: str
    url: str
    location: Optional[str] = None
    salary_min: Optional[float] = None      # AUD 年薪
    salary_max: Optional[float] = None
    salary_raw: Optional[str] = None        # 原始薪资字符串,方便排查解析错误
    work_type: Optional[str] = None
    posted_date: Optional[str] = None
    description: str = ""
    duplicate_urls: list[str] = field(default_factory=list)
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = canonical_job_id(self.company, self.title)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duplicate_urls"] = "; ".join(self.duplicate_urls)
        return d

    def completeness(self) -> int:
        """信息完整度打分,去重时用来决定保留哪一条。"""
        score = 0
        if self.salary_min is not None:
            score += 3
        if self.description and len(self.description) > 200:
            score += 2
        if self.location:
            score += 1
        if self.work_type:
            score += 1
        if self.posted_date:
            score += 1
        return score


# ---------------------------------------------------------------- 归一化

_COMPANY_SUFFIXES = re.compile(
    r"\b(pty\.?\s*ltd\.?|pty|ltd\.?|limited|inc\.?|incorporated|llc|"
    r"group|holdings|australia|au|corp\.?|corporation)\b",
    re.I,
)
_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def norm_company(name: Optional[str]) -> str:
    if not name:
        return ""
    s = name.lower()
    s = _PUNCT.sub(" ", s)
    s = _COMPANY_SUFFIXES.sub(" ", s)
    return _WS.sub(" ", s).strip()


_TITLE_NOISE = re.compile(
    r"\b(m/f/d|f/m/d|urgent|hiring|immediate start|new|hot job|"
    r"apply now|full[\s-]?time|part[\s-]?time|permanent|contract)\b",
    re.I,
)
_TITLE_ANNOTATION = re.compile(r"[\(\[]([^\)\]]+)[\)\]]")
_LOCATION_ANNOTATION = re.compile(
    r"\b(?:sydney|melbourne|brisbane|perth|adelaide|canberra|hobart|darwin|"
    r"vic|nsw|qld|wa|sa|act|tas|nt|australia|au|remote|hybrid|onsite|"
    r"req(?:uisition)?|ref(?:erence)?|job|id)\b|\d",
    re.I,
)


def _title_annotation(match: re.Match[str]) -> str:
    """Drop location/reference annotations but keep role differentiators."""
    content = match.group(1).strip()
    return " " if _LOCATION_ANNOTATION.search(content) else f" {content} "


def norm_title(title: Optional[str]) -> str:
    if not title:
        return ""
    s = title.lower()
    # 去掉地点/req 编号括号,保留 "(Back End)"、"(Model Training)" 这类
    # 能区分同一公司不同岗位的角色限定词。
    s = _TITLE_ANNOTATION.sub(_title_annotation, s)
    s = _TITLE_NOISE.sub(" ", s)
    s = _PUNCT.sub(" ", s)
    # 常见同义词归一
    s = re.sub(r"\bsr\b|\bsnr\b", "senior", s)
    s = re.sub(r"\bjr\b", "junior", s)
    # developer / engineer / programmer 在澳洲 JD 里基本混用,
    # 同一家公司的 "Backend Developer" 和 "Backend Engineer" 几乎一定是同一个岗,
    # 统一成一个 token 才能被去重抓到
    s = re.sub(r"\b(dev|devs|developer|developers|programmer|eng|engineers)\b",
               "engineer", s)
    return _WS.sub(" ", s).strip()


def norm_location(loc: Optional[str]) -> str:
    if not loc:
        return ""
    s = loc.lower()
    s = re.sub(r"\b(?:australia|au)\b", " ", s)
    s = _PUNCT.sub(" ", s)
    # 州名归一
    for full, abbr in [
        ("new south wales", "nsw"), ("victoria", "vic"),
        ("queensland", "qld"), ("western australia", "wa"),
        ("south australia", "sa"), ("tasmania", "tas"),
        ("australian capital territory", "act"),
        ("northern territory", "nt"),
    ]:
        s = s.replace(full, abbr)
    return _WS.sub(" ", s).strip()


def job_identity_key(company: Optional[str], title: Optional[str]) -> str:
    """Return the source-independent identity used to group one advertised role.

    The key deliberately excludes source and location.  A single employer can
    publish the same role on several boards with different location formatting;
    the current search scope is one metro area, so those records should share
    one application identity.
    """
    return f"{norm_company(company)}|{norm_title(title)}"


def _job_id_from_key(key: str) -> str:
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def canonical_job_id(
    company: Optional[str],
    title: Optional[str],
    location: Optional[str] = None,
) -> str:
    """Return the stable cross-source identity for a job.

    Location is intentionally not part of this identity. The same company and
    role may be reported with a blank location on one source and a detailed
    Melbourne/Victoria location on another, so including it recreates duplicate
    dashboard rows. This also means same-company/same-title roles in different
    cities merge; the current search scope is Melbourne only, and this must be
    reassessed before expanding to multiple cities.

    ``location`` remains an optional compatibility argument for callers written
    against WF-101; it is deliberately ignored.
    """
    del location
    return _job_id_from_key(job_identity_key(company, title))


def legacy_job_id(company: Optional[str], title: Optional[str], url: Optional[str]) -> str:
    """Return the pre-WF-101 URL-based identity for migration tooling."""
    key = f"{norm_company(company)}|{norm_title(title)}|{url or ''}"
    return _job_id_from_key(key)


# ---------------------------------------------------------------- 薪资解析

# 匹配 120k / 120,000 / 120000 / $120K
_NUM = r"\$?\s*(\d[\d,\.]*)\s*([kK])?"
_RANGE_RE = re.compile(_NUM + r"\s*(?:-|–|—|to)\s*" + _NUM)
_SINGLE_RE = re.compile(_NUM)

_HOURLY_HINT = re.compile(r"\b(per hour|/\s*hr|hourly|p\.?h\.?)\b", re.I)
_DAILY_HINT = re.compile(r"\b(per day|/\s*day|daily|p\.?d\.?)\b", re.I)
_MONTHLY_HINT = re.compile(r"\b(per month|/\s*month|monthly)\b", re.I)

# 澳洲全职年薪的合理区间,超出就认为解析错了
_ANNUAL_MIN, _ANNUAL_MAX = 30_000, 1_000_000


def _to_number(raw: str, k_flag: Optional[str],
               assume_thousands: bool) -> Optional[float]:
    """assume_thousands 只在年薪语境下开启。

    年薪写 "120" 几乎一定是 120k;但时薪写 "65" 就是 65 块,
    不区分的话 "$800 per day" 会被算成 80 万日薪。
    """
    try:
        v = float(raw.replace(",", ""))
    except ValueError:
        return None
    if k_flag:
        v *= 1000
    elif assume_thousands and v < 1000:
        v *= 1000
    return v


def parse_salary(text: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """把各平台的薪资字符串统一成 AUD 年薪 (min, max)。

    SEEK:    "$120,000 - $140,000 per annum + super"
    Indeed:  "$120,000 - $140,000 a year"
    LinkedIn: 经常直接没有
    解析不出或超出合理范围 → (None, None)
    """
    if not text or not str(text).strip():
        return None, None
    s = str(text)

    # 换算倍数:澳洲标准全职 38h/week
    if _HOURLY_HINT.search(s):
        mult = 38 * 52
    elif _DAILY_HINT.search(s):
        mult = 5 * 52
    elif _MONTHLY_HINT.search(s):
        mult = 12
    else:
        mult = 1

    annual = mult == 1
    lo = hi = None
    m = _RANGE_RE.search(s)
    if m:
        lo = _to_number(m.group(1), m.group(2), annual)
        hi = _to_number(m.group(3), m.group(4), annual)
    else:
        m = _SINGLE_RE.search(s)
        if m:
            lo = hi = _to_number(m.group(1), m.group(2), annual)

    if lo is None:
        return None, None

    if not annual:
        lo *= mult
        hi = hi * mult if hi else None

    if hi and hi < lo:
        lo, hi = hi, lo

    # 超出合理范围就认为解析失败,宁可留空
    if not (_ANNUAL_MIN <= lo <= _ANNUAL_MAX):
        return None, None
    if hi and not (_ANNUAL_MIN <= hi <= _ANNUAL_MAX):
        hi = None

    return lo, hi

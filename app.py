"""
Team11 - 부산튜브 시민 반응 분석 대시보드 (3단계 최종본)

기능
1) YouTube Data API v3로 영상 메타데이터·댓글 수집 (작성자 익명화, 개인정보 마스킹)
2) 영상 유형 분류 (정책 안내 / 지역 축제·관광 / 숏폼·예능)
3) Kiwi 형태소 분석 기반 키워드 추출 + 사전 기반 감성 분석
4) 유형별 비교 시각화, 워드클라우드, 카이제곱 검정, 만위트니 U 검정
실행: streamlit run app.py
"""
import math
import os
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:  # 워드클라우드는 선택 기능 (설치 안 돼 있으면 막대그래프만 표시)
    from wordcloud import WordCloud
except Exception:
    WordCloud = None

st.set_page_config(page_title="부산튜브 시민 반응 분석", layout="wide")

DATA_DIR = Path("data")
VIDEOS_CSV = DATA_DIR / "videos_sample.csv"
COMMENTS_CSV = DATA_DIR / "comments_sample.csv"

CATEGORIES = ["정책 안내", "지역 축제·관광", "숏폼·예능", "기타"]
SENT_ORDER = ["긍정", "중립", "부정"]
SENT_COLORS = {"긍정": "#4C9F70", "중립": "#B0B7C3", "부정": "#D1495B"}

# ---------------------------------------------------------------------------
# 1. 영상 유형 분류 (제목 키워드 + 영상 길이)
# ---------------------------------------------------------------------------
SHORT_TAGS = ["shorts", "쇼츠", "숏"]  # 제목에 있으면 무조건 숏폼 (영상 길이 60초 이하도 숏폼)
KEYWORDS = {
    "지역 축제·관광": ["축제", "관광", "여행", "명소", "페스티벌", "불꽃", "해수욕장", "투어",
                   "맛집", "야경", "벚꽃", "엑스포", "마켓", "콘서트", "공연",
                   "박람회", "락페", "체전", "마라톤", "모두의 바다"],
    "숏폼·예능": ["예능", "챌린지", "댄스", "댄댄댄", "브이로그", "vlog", "웹예능",
                "라이브", "비하인드", "밈", "먹방", "해봐서", "가봐서", "리그", "덕후", "멘터리",
                "들락날락", "위켄두잇", "수다", "부캉이", "시네마", "자급자족", "우리 부산에 가자",
                "함께라 참", "최애", "갓생", "골든벨"],
    "정책 안내": ["정책", "지원", "모집", "신청", "안내", "시정", "브리핑", "대책", "회견",
                "발표", "조례", "복지", "청년", "예산", "사업", "시민", "돌봄", "주택", "교통", "행정",
                "회의", "간부", "타운홀", "미팅", "민생", "비상조치", "수돗물", "법원", "투자",
                "온에어", "슬기로운 부산생활", "북극항로", "장애인", "교육", "교재", "제안", "발대식"],
}


def apply_categories(df):
    """영상 DataFrame의 유형(category) 열을 제목·길이로 새로 계산한다.
    수집 결과가 캐시에 남아 있어도 최신 분류 규칙이 적용되도록 수집·불러오기 직후마다 호출한다."""
    df = df.copy()
    secs = df["duration_sec"] if "duration_sec" in df.columns else [0] * len(df)
    df["category"] = [classify_video(t, s) for t, s in zip(df["title"], secs)]
    return df


def parse_duration(iso):
    """ISO 8601 길이(PT1M30S)를 초 단위 정수로 바꾼다."""
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def classify_video(title, seconds):
    """입력: 영상 제목, 길이(초) / 출력: 영상 유형 문자열.
    순서: ① 쇼츠(60초 이하 또는 제목 태그) ② 축제·관광 ③ 예능 시리즈 ④ 정책 안내 ⑤ 기타.
    유튜브 제목이 자모가 분리된 형태일 수 있어 NFC로 정규화한 뒤 비교한다."""
    t = unicodedata.normalize("NFC", title or "").lower()
    if 0 < seconds <= 60 or any(k in t for k in SHORT_TAGS):
        return "숏폼·예능"
    for cat in ("지역 축제·관광", "숏폼·예능", "정책 안내"):
        if any(k in t for k in KEYWORDS[cat]):
            return cat
    return "기타"


# ---------------------------------------------------------------------------
# 2. 데이터 수집 (YouTube Data API v3)
# ---------------------------------------------------------------------------
def get_api_key():
    """secrets.toml 또는 환경변수에서 API 키를 읽는다. 키는 코드에 쓰지 않는다."""
    try:
        return st.secrets["YOUTUBE_API_KEY"]
    except Exception:
        return os.environ.get("YOUTUBE_API_KEY")


def resolve_channel(youtube, query):
    """채널 ID / @핸들 / 이름 → (채널 제목, 업로드 재생목록 ID). 못 찾으면 None."""
    query = query.strip()
    if query.startswith("UC"):
        res = youtube.channels().list(part="snippet,contentDetails", id=query).execute()
    elif query.startswith("@"):
        res = youtube.channels().list(part="snippet,contentDetails", forHandle=query).execute()
    else:
        found = youtube.search().list(part="snippet", q=query, type="channel", maxResults=1).execute()
        if not found.get("items"):
            return None
        cid = found["items"][0]["snippet"]["channelId"]
        res = youtube.channels().list(part="snippet,contentDetails", id=cid).execute()
    if not res.get("items"):
        return None
    item = res["items"][0]
    return item["snippet"]["title"], item["contentDetails"]["relatedPlaylists"]["uploads"]


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_videos(api_key, uploads_id, start_date, max_videos):
    """입력: 업로드 재생목록 ID, 수집 시작일('YYYY-MM-DD'), 최대 영상 수 /
    출력: 시작일 이후 영상 메타데이터 DataFrame(유형 포함).
    업로드 목록은 최신순이라 시작일보다 오래된 영상이 나오면 거기서 멈춘다."""
    youtube = build("youtube", "v3", developerKey=api_key)
    ids, token, done = [], None, False
    while len(ids) < max_videos and not done:
        res = youtube.playlistItems().list(
            part="contentDetails", playlistId=uploads_id, maxResults=50, pageToken=token).execute()
        for i in res["items"]:
            published = i["contentDetails"].get("videoPublishedAt", "")[:10]
            if published and published < start_date:
                done = True
                break
            ids.append(i["contentDetails"]["videoId"])
            if len(ids) >= max_videos:
                break
        token = res.get("nextPageToken")
        if not token:
            break

    rows = []
    for i in range(0, len(ids), 50):
        res = youtube.videos().list(part="snippet,statistics,contentDetails",
                                    id=",".join(ids[i:i + 50])).execute()
        for v in res["items"]:
            s = v.get("statistics", {})
            sec = parse_duration(v["contentDetails"].get("duration"))
            title = v["snippet"]["title"]
            rows.append({
                "video_id": v["id"], "title": title,
                "published_at": v["snippet"]["publishedAt"][:10],
                "duration_sec": sec,
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "comment_count": int(s.get("commentCount", 0)),
                "category": classify_video(title, sec),
            })
    return pd.DataFrame(rows)


def sample_by_category(videos, cap):
    """입력: 영상 DataFrame, 유형별 최대 영상 수 / 출력: 유형마다 cap개까지 무작위(고정 시드)로 뽑은 DataFrame.
    특정 유형 영상이 너무 많아 분석이 한쪽으로 쏠리는 것을 막는다."""
    parts = [g.sample(min(len(g), cap), random_state=42) for _, g in videos.groupby("category")]
    return pd.concat(parts, ignore_index=True) if parts else videos


def mask_pii(text):
    """댓글 속 전화번호·이메일·@멘션을 가린다 (개인정보 보호)."""
    text = re.sub(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}", "[전화번호]", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[이메일]", text)
    return re.sub(r"@[\w.\-가-힣]+", "@user", text)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_comments(api_key, video_ids, per_video):
    """입력: 영상 ID 목록, 영상당 최대 댓글 수 / 출력: 익명화된 댓글 DataFrame.
    작성자 닉네임은 user_001 형태로 바꾸고 원본은 저장하지 않는다."""
    youtube = build("youtube", "v3", developerKey=api_key)
    rows, alias = [], {}
    for vid in video_ids:
        got, token = 0, None
        while got < per_video:
            try:
                res = youtube.commentThreads().list(
                    part="snippet", videoId=vid, maxResults=min(100, per_video - got),
                    order="time", textFormat="plainText", pageToken=token).execute()
            except HttpError:  # 댓글이 막힌 영상은 건너뜀
                break
            for it in res["items"]:
                c = it["snippet"]["topLevelComment"]["snippet"]
                name = c.get("authorDisplayName", "")
                if name not in alias:
                    alias[name] = f"user_{len(alias) + 1:03d}"
                rows.append({
                    "video_id": vid, "author": alias[name],
                    "comment": mask_pii(c["textDisplay"]),
                    "likes": c.get("likeCount", 0),
                    "published_at": c["publishedAt"][:10],
                })
                got += 1
            token = res.get("nextPageToken")
            if not token:
                break
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3. 텍스트 분석 (Kiwi 형태소 분석 + 감성 사전)
# ---------------------------------------------------------------------------
POSITIVE = {
    "좋", "훌륭", "감사", "고맙", "응원", "최고", "멋지", "대박", "행복", "사랑", "재미있", "재밌",
    "기대", "축하", "유익", "도움", "편리", "친절", "깔끔", "예쁘", "귀엽", "웃기", "힘내", "화이팅",
    "파이팅", "수고", "잘하", "칭찬", "존경", "감동", "만족", "따뜻", "든든", "환영", "굿", "추천",
    "반갑", "신나", "기쁘", "자랑", "희망", "최선", "열정", "노력", "화목", "안전", "쾌적", "혁신",
}
NEGATIVE = {
    "싫", "나쁘", "최악", "짜증", "화나", "분노", "실망", "불편", "불만", "문제", "심각", "불친절",
    "세금", "낭비", "쓰레기", "혈세", "무능", "답답", "한심", "거짓", "사퇴", "반대", "폐지", "위험",
    "피해", "부족", "논란", "비판", "아깝", "쓸데없", "엉망", "부끄럽", "창피", "불안", "걱정", "우려",
    "비싸", "힘들", "민폐", "무책임", "불법", "혼잡", "지저분", "시끄럽", "황당", "어이없", "허술", "비리",
}
NEGATORS = {"않", "못", "안", "아니", "없"}
NEG_ONLY_AFTER = {"않", "못"}  # 감성어 뒤에 붙어 부정 (예: 좋지 않다)
STOP_NOUNS = {"것", "수", "등", "때", "이번", "하나", "사람", "우리", "저희", "생각", "말", "분",
              "년", "오늘", "내용", "정도", "부분", "번", "곳", "거", "점", "중"}


@st.cache_resource(show_spinner="형태소 분석기 불러오는 중...")
def get_kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()


def analyze_text(kiwi, text):
    """입력: 댓글 문자열 / 출력: (명사 리스트, 감성 점수).
    감성 점수 = 긍정어 수 - 부정어 수 (직전 '안/못', 직후 '않/못' 부정어가 있으면 극성 반전)."""
    tokens = kiwi.tokenize(text)
    nouns, score = [], 0
    for i, t in enumerate(tokens):
        if t.tag in ("NNG", "NNP") and len(t.form) >= 2 and t.form not in STOP_NOUNS:
            nouns.append(t.form)
        if t.form in POSITIVE or t.form in NEGATIVE:
            pol = 1 if t.form in POSITIVE else -1
            prev_neg = i > 0 and tokens[i - 1].form in {"안", "못"}
            next_neg = any(x.form in NEG_ONLY_AFTER for x in tokens[i + 1:i + 5])
            if prev_neg or next_neg:
                pol = -pol
            score += pol
    return nouns, score


@st.cache_data(show_spinner="댓글을 분석하는 중...")
def analyze_comments(comments):
    """입력: 댓글 DataFrame / 출력: nouns(명사 리스트), score, sentiment(긍정·중립·부정) 열이 추가된 DataFrame."""
    kiwi = get_kiwi()
    df = comments.copy()
    results = [analyze_text(kiwi, str(t)) for t in df["comment"]]
    df["nouns"] = [r[0] for r in results]
    df["score"] = [r[1] for r in results]
    df["sentiment"] = df["score"].apply(lambda s: "긍정" if s > 0 else ("부정" if s < 0 else "중립"))
    return df


def chi_square_test(table):
    """입력: 2×k 분할표(DataFrame) / 출력: (카이제곱 값, 자유도, p값). 자유도 1 또는 2만 지원."""
    table = table.loc[:, table.sum() > 0]
    obs = table.values.astype(float)
    exp = obs.sum(axis=1, keepdims=True) * obs.sum(axis=0, keepdims=True) / obs.sum()
    chi2 = float(((obs - exp) ** 2 / exp).sum())
    df = (obs.shape[0] - 1) * (obs.shape[1] - 1)
    if df == 1:
        p = math.erfc(math.sqrt(chi2 / 2))
    elif df == 2:
        p = math.exp(-chi2 / 2)
    else:
        return chi2, df, float("nan")
    return chi2, df, p


def mann_whitney_u(a, b):
    """입력: 두 집단의 값 목록 / 출력: (U 값, p값).
    두 집단의 순위 합으로 U를 구하고 정규분포 근사로 p값을 계산한다. 동순위 보정 포함."""
    a = pd.Series(a).dropna()
    b = pd.Series(b).dropna()
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return float("nan"), float("nan")
    allv = pd.concat([a, b], ignore_index=True)
    ranks = allv.rank(method="average")
    u1 = ranks.iloc[:n1].sum() - n1 * (n1 + 1) / 2
    n = n1 + n2
    ties = allv.value_counts().astype(float)
    tie_term = float((ties ** 3 - ties).sum()) / (n * (n - 1))
    sigma = math.sqrt(n1 * n2 / 12 * ((n + 1) - tie_term))
    if sigma == 0:
        return float(u1), float("nan")
    z = (u1 - n1 * n2 / 2) / sigma
    return float(u1), math.erfc(abs(z) / math.sqrt(2))


def find_korean_font():
    """워드클라우드에 쓸 한글 폰트 경로를 찾는다 (프로젝트 fonts 폴더 → 시스템 폰트 순)."""
    candidates = list(Path("fonts").glob("*.[to]tf")) + [
        Path(p) for p in (
            "C:/Windows/Fonts/malgun.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/Library/Fonts/AppleGothic.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


def keyword_counts(df, exclude, top_n=30):
    """입력: 분석된 댓글 DataFrame, 제외 단어 집합 / 출력: 단어 빈도 DataFrame(상위 top_n)."""
    counts = {}
    for nouns in df["nouns"]:
        for w in nouns:
            if w not in exclude:
                counts[w] = counts.get(w, 0) + 1
    out = pd.DataFrame(sorted(counts.items(), key=lambda x: -x[1])[:top_n], columns=["키워드", "빈도"])
    return out


def show_wordcloud(freq_df):
    """빈도 DataFrame을 워드클라우드 이미지로 보여 준다 (불가능하면 안내만). 글자는 모두 가로로 배치한다."""
    font = find_korean_font()
    if WordCloud is None or font is None or freq_df.empty:
        st.caption("워드클라우드를 만들 수 없어 빈도 차트만 표시합니다. (wordcloud 미설치 또는 한글 폰트 없음)")
        return
    wc = WordCloud(font_path=font, width=800, height=420, background_color="white",
                   prefer_horizontal=1.0)
    wc.generate_from_frequencies(dict(zip(freq_df["키워드"], freq_df["빈도"])))
    st.image(wc.to_array())


# ---------------------------------------------------------------------------
# 4. 데이터 불러오기 / 저장
# ---------------------------------------------------------------------------
def save_samples(videos, comments):
    """수집 결과(익명화됨)를 data 폴더에 저장해 두면 키 없이도 앱을 볼 수 있다."""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        videos.to_csv(VIDEOS_CSV, index=False, encoding="utf-8-sig")
        comments.to_csv(COMMENTS_CSV, index=False, encoding="utf-8-sig")
        return True
    except Exception:
        return False


def load_samples():
    """저장된 샘플 CSV가 있으면 (영상, 댓글) DataFrame을 반환, 없으면 None."""
    if VIDEOS_CSV.exists() and COMMENTS_CSV.exists():
        return pd.read_csv(VIDEOS_CSV), pd.read_csv(COMMENTS_CSV)
    return None


# ---------------------------------------------------------------------------
# 5. 화면
# ---------------------------------------------------------------------------
st.title("부산튜브 시민 반응 분석 대시보드")
st.caption("Team11 | 부산시 공식 유튜브 콘텐츠 유형별 시민 참여도·감성 반응 분석")

api_key = get_api_key()

with st.sidebar:
    st.header("① 데이터 수집")
    channel_q = st.text_input("채널 (이름, @핸들 또는 채널 ID)", value="부산튜브")
    start_date = st.date_input("수집 시작일 (이 날짜 이후 업로드 영상)",
                               value=date.today() - timedelta(days=365 * 3), max_value=date.today())
    max_videos = st.slider("기간 내 영상 최대 개수", 50, 2000, 500, step=50)
    cap_per_cat = st.slider("유형별 댓글 수집 영상 수", 10, 100, 30, step=5)
    per_video = st.slider("영상당 최대 댓글 수", 10, 300, 100, step=10)
    run = st.button("데이터 수집 시작", type="primary", disabled=not api_key)
    if not api_key:
        st.caption("API 키가 없어 저장된 샘플 데이터만 볼 수 있습니다.")

if run:
    try:
        youtube = build("youtube", "v3", developerKey=api_key)
        ch = resolve_channel(youtube, channel_q)
        if ch is None:
            st.warning("채널을 찾지 못했습니다. 채널 ID(UC로 시작)를 직접 입력해 보세요.")
            st.stop()
        ch_title, uploads_id = ch
        with st.spinner("영상 정보를 수집하는 중..."):
            pool = apply_categories(fetch_videos(api_key, uploads_id, str(start_date), max_videos))
        if pool.empty:
            st.warning("선택한 기간에 업로드된 영상이 없습니다. 수집 시작일을 더 과거로 바꿔 보세요.")
            st.stop()
        v = sample_by_category(pool, cap_per_cat)
        with st.spinner("댓글을 수집하는 중..."):
            c = fetch_comments(api_key, tuple(v["video_id"]), per_video)
        saved = save_samples(v, c)
        st.session_state["data"] = (ch_title, v, c)
        st.session_state["pool"] = pool
        st.sidebar.success("수집 완료" + (" (data 폴더에 저장됨)" if saved else ""))
    except HttpError as e:
        st.error(f"YouTube API 오류: {e}")
        st.stop()

if "data" not in st.session_state:
    loaded = load_samples()
    if loaded is None:
        st.info("왼쪽 사이드바에서 '데이터 수집 시작'을 눌러 주세요. (API 키 필요)")
        st.stop()
    st.session_state["data"] = ("저장된 샘플 데이터", loaded[0], loaded[1])

source_name, videos, comments = st.session_state["data"]
videos = videos.copy()
videos["published_at"] = pd.to_datetime(videos["published_at"])
videos = apply_categories(videos)

if comments.empty:
    st.warning("수집된 댓글이 없습니다. 다른 채널이나 설정으로 다시 수집해 보세요.")
    st.stop()

analyzed = analyze_comments(comments)
analyzed = analyzed.merge(videos[["video_id", "title", "category"]], on="video_id", how="left")

# ---- 사이드바 필터 ----
with st.sidebar:
    st.header("② 분석 필터")
    cat_choice = st.selectbox("영상 카테고리", ["전체"] + CATEGORIES)
    dmin, dmax = videos["published_at"].min().date(), videos["published_at"].max().date()
    if dmin < dmax:
        date_range = st.slider("분석 기간 (영상 업로드일)", dmin, dmax, (dmin, dmax))
    else:
        date_range = (dmin, dmax)
    exclude_txt = st.text_input("키워드에서 제외할 단어 (쉼표로 구분)", value="영상,댓글,부산,시장,정말,진짜,감사")
    exclude = {w.strip() for w in exclude_txt.split(",") if w.strip()}

mask_v = (videos["published_at"].dt.date >= date_range[0]) & (videos["published_at"].dt.date <= date_range[1])
if cat_choice != "전체":
    mask_v &= videos["category"] == cat_choice
f_videos = videos[mask_v]
f_comments = analyzed[analyzed["video_id"].isin(f_videos["video_id"])]

st.subheader(f"분석 대상: {source_name}")
if "pool" in st.session_state:
    pool = st.session_state["pool"].copy()
    pool["연도"] = pd.to_datetime(pool["published_at"]).dt.year
    with st.expander(f"수집 기간 내 전체 영상 현황 (총 {len(pool):,}개 중 유형별로 뽑아 댓글을 분석)"):
        st.dataframe(pd.crosstab(pool["연도"], pool["category"]))
if f_videos.empty or f_comments.empty:
    st.warning("선택한 조건에 맞는 영상 또는 댓글이 없습니다. 필터를 바꿔 보세요.")
    st.stop()

# ---- 요약 지표 ----
pos_ratio = (f_comments["sentiment"] == "긍정").mean() * 100
neg_ratio = (f_comments["sentiment"] == "부정").mean() * 100
m1, m2, m3, m4 = st.columns(4)
m1.metric("분석 영상 수", f"{len(f_videos):,}")
m2.metric("수집된 댓글 수", f"{len(f_comments):,}")
m3.metric("긍정 댓글 비율", f"{pos_ratio:.1f}%")
m4.metric("부정 댓글 비율", f"{neg_ratio:.1f}%")

# ---- 중단: 유형별 비교 + 감성 비율 ----
st.markdown("### 1. 영상 유형별 참여도와 감성 반응")
left, right = st.columns(2)
with left:
    metric_label = st.radio("비교 지표", ["조회수", "좋아요 수", "댓글 수"], horizontal=True)
    col = {"조회수": "views", "좋아요 수": "likes", "댓글 수": "comment_count"}[metric_label]
    by_cat = f_videos.groupby("category").agg(
        평균=(col, "mean"), 중앙값=(col, "median"), 영상수=("video_id", "count")).reset_index()
    fig = px.bar(by_cat, x="category", y="평균", text="영상수",
                 labels={"category": "영상 유형", "평균": f"평균 {metric_label}"},
                 title=f"유형별 평균 {metric_label} (막대 위 숫자 = 영상 수)")
    fig.update_traces(texttemplate="%{text}개", textposition="outside")
    st.plotly_chart(fig)
    st.dataframe(by_cat.rename(columns={"category": "영상 유형"}).round(0))
with right:
    sent_counts = f_comments["sentiment"].value_counts().reindex(SENT_ORDER).fillna(0).reset_index()
    sent_counts.columns = ["감성", "댓글 수"]
    fig = px.pie(sent_counts, names="감성", values="댓글 수", color="감성",
                 color_discrete_map=SENT_COLORS, title="댓글 감성 비율", hole=0.35)
    st.plotly_chart(fig)

# 유형별 감성 비율 (100% 막대)
cat_sent = pd.crosstab(f_comments["category"], f_comments["sentiment"]).reindex(columns=SENT_ORDER, fill_value=0)
cat_pct = cat_sent.div(cat_sent.sum(axis=1), axis=0).mul(100).round(1).reset_index().melt(
    id_vars="category", var_name="감성", value_name="비율(%)")
fig = px.bar(cat_pct, x="비율(%)", y="category", color="감성", orientation="h", barmode="stack",
             color_discrete_map=SENT_COLORS, category_orders={"감성": SENT_ORDER},
             labels={"category": "영상 유형"}, title="영상 유형별 감성 비율")
st.plotly_chart(fig)

# 카이제곱 검정 (정책 안내 vs 숏폼·예능)
if {"정책 안내", "숏폼·예능"} <= set(cat_sent.index):
    chi2, dof, p = chi_square_test(cat_sent.loc[["정책 안내", "숏폼·예능"]])
    verdict = "통계적으로 유의한 차이가 있음" if p < 0.05 else "통계적으로 유의한 차이가 있다고 보기 어려움"
    st.info(f"**정책 안내 vs 숏폼·예능 감성 분포 카이제곱 검정**: χ²={chi2:.2f}, 자유도={dof}, "
            f"p={p:.4f} → {verdict} (유의수준 0.05)")
else:
    st.caption("정책 안내와 숏폼·예능 영상이 모두 포함되면 감성 분포 차이 검정 결과가 표시됩니다.")

# 참여도 차이 검정 (정책 안내 vs 숏폼·예능)
if {"정책 안내", "숏폼·예능"} <= set(f_videos["category"]):
    ga = f_videos[f_videos["category"] == "정책 안내"][col]
    gb = f_videos[f_videos["category"] == "숏폼·예능"][col]
    u_val, p_mw = mann_whitney_u(ga, gb)
    if math.isnan(p_mw):
        st.caption("영상 수가 부족해 참여도 차이 검정 결과를 계산할 수 없습니다.")
    else:
        verdict2 = "통계적으로 유의한 차이가 있음" if p_mw < 0.05 else "통계적으로 유의한 차이가 있다고 보기 어려움"
        st.info(f"**정책 안내 vs 숏폼·예능 {metric_label} 만위트니 U 검정**: U={u_val:.1f}, "
                f"p={p_mw:.4f} → {verdict2} (유의수준 0.05) / "
                f"중앙값 {ga.median():,.0f} 대 {gb.median():,.0f}")

# ---- 하단: 키워드 / 건의·불만 / 원문 ----
st.markdown("### 2. 시민들이 많이 언급한 키워드와 건의·불만")
tab_all, tab_neg, tab_raw = st.tabs(["전체 키워드", "부정 댓글 키워드·원문", "수집 데이터 확인"])

with tab_all:
    kw = keyword_counts(f_comments, exclude)
    c1, c2 = st.columns(2)
    with c1:
        show_wordcloud(kw)
    with c2:
        if kw.empty:
            st.write("표시할 키워드가 없습니다.")
        else:
            fig = px.bar(kw.head(15).iloc[::-1], x="빈도", y="키워드", orientation="h", title="키워드 빈도 TOP 15")
            st.plotly_chart(fig)

with tab_neg:
    neg = f_comments[f_comments["sentiment"] == "부정"]
    if neg.empty:
        st.write("부정으로 분류된 댓글이 없습니다.")
    else:
        kw_neg = keyword_counts(neg, exclude, top_n=15)
        fig = px.bar(kw_neg.iloc[::-1], x="빈도", y="키워드", orientation="h", title="부정 댓글 키워드 TOP 15")
        st.plotly_chart(fig)
        st.write("공감(좋아요)을 많이 받은 부정 댓글")
        st.dataframe(neg.sort_values("likes", ascending=False)[["category", "title", "comment", "likes"]]
                     .head(20).rename(columns={"category": "유형", "title": "영상", "comment": "댓글", "likes": "좋아요"}))

with tab_raw:
    st.write("영상 메타데이터")
    st.dataframe(f_videos.drop(columns=["duration_sec"], errors="ignore"))
    st.write("댓글 샘플 (작성자 익명화됨, 개인정보 마스킹됨)")
    st.dataframe(f_comments[["author", "category", "comment", "sentiment", "likes"]].head(100))

st.divider()
st.caption("※ 감성 분석은 직접 만든 긍정·부정 단어 사전과 Kiwi 형태소 분석을 이용한 단순 방식이라 "
           "반어법·문맥은 반영하지 못합니다. 영상 유형도 제목·길이 키워드로 자동 분류한 값입니다.")

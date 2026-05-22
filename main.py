import streamlit as st
import sqlite3
import hashlib
import json
from datetime import datetime
from google import genai
from google.genai import types
from PIL import Image
import io
import os
import secrets

# 데이터베이스 파일 설정
DB_NAME = "nutrilog.db"

# ------------------------------------------------------------------
# google gemini API 키
# ------------------------------------------------------------------
GEMINI_API_KEY = "AIzaSyBSHbJvwAiQ0fjM2IXVZokuEO9cyQI8lvk"

# ------------------------------------------------------------------
# 자동 로그인 관련 브라우저 Storage JS 헬퍼 함수 (표준형)
# ------------------------------------------------------------------
def save_login_to_local_storage(username, token):
    """로그인 성공 시 식별 정보와 토큰을 브라우저 localStorage에 안전하게 보관합니다."""
    js_code = f"""
    <script>
        localStorage.setItem('auto_user', '{username}');
        localStorage.setItem('auto_token', '{token}');
    </script>
    """
    st.components.v1.html(js_code, height=0)

def clear_login_from_local_storage():
    """로그아웃 버튼 클릭 시 브라우저 내 자동 로그인 관련 값을 완전히 소각합니다."""
    js_code = """
    <script>
        localStorage.removeItem('auto_user');
        localStorage.removeItem('auto_token');
    </script>
    """
    st.components.v1.html(js_code, height=0)

def trigger_auto_login_check():
    """브라우저의 localStorage를 조회하여 토큰이 발견되면 주소창 쿼리 매개변수로 주입해 서버 검증을 요청합니다."""
    js_code = """
    <script>
        const user = localStorage.getItem('auto_user');
        const token = localStorage.getItem('auto_token');
        if (user && token) {
            const url = new URL(window.location.href);
            if (!url.searchParams.has('autouser')) {
                url.searchParams.set('autouser', user);
                url.searchParams.set('autotoken', token);
                window.location.href = url.toString();
            }
        }
    </script>
    """
    st.components.v1.html(js_code, height=0)

# ------------------------------------------------------------------
# 1. 데이터베이스(DB) 및 다중 사용자 보안 관리 함수
# ------------------------------------------------------------------
def get_db_connection():
    """
    SQLite 데이터베이스 연결 객체를 생성합니다.
    동시성 잠금 에러(database is locked)를 해결하기 위해 timeout과 WAL 모드를 설정합니다.
    """
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """앱에 필요한 데이터베이스 테이블을 생성하고, 누락되거나 어긋난 스키마가 있다면 자동으로 감지하여 복구합니다."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1) 회원 정보 테이블 생성 (비밀번호는 암호화하여 저장)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL
            )
        ''')
        
        # 2) 기존 테이블이 존재하는지 확인하고 칼럼 구조 확인
        cursor.execute("PRAGMA table_info(food_logs)")
        existing_columns = [col[1] for col in cursor.fetchall()]
        
        # 만약 기존 테이블에 불필요한 'date' 제약조건이 있거나 핵심 구조가 어긋난 구버전 테이블이 발견되면 자동 재구축
        if existing_columns and ('date' in existing_columns or 'timestamp' not in existing_columns):
            cursor.execute("DROP TABLE IF EXISTS food_logs")
            existing_columns = []
            
        # 3) 식단 기록 테이블 생성 (기본 구조 정의)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS food_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT DEFAULT 'guest',
                timestamp TEXT,
                food_name TEXT,
                calories INTEGER,
                macros_json TEXT,
                image BLOB
            )
        ''')
        
        # 4) 자동 로그인 정보 세션을 기록할 전용 테이블 생성
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                username TEXT,
                token TEXT PRIMARY KEY,
                created_at TEXT
            )
        ''')
        
        # 다시 칼럼 구조 파악 (새로 만들어졌거나 유효한 기존 칼럼 정보 로드)
        cursor.execute("PRAGMA table_info(food_logs)")
        existing_columns = [col[1] for col in cursor.fetchall()]
        
        # 앱 구동에 꼭 필요한 칼럼 목록 및 타입 정의
        required_columns = {
            "username": "TEXT DEFAULT 'guest'",
            "timestamp": "TEXT",
            "food_name": "TEXT",
            "calories": "INTEGER",
            "macros_json": "TEXT",
            "image": "BLOB"
        }
        
        # 누락된 칼럼이 발견되면, 테이블을 유지한 채 자동으로 추가(ALTER TABLE)
        for col_name, col_type in required_columns.items():
            if col_name not in existing_columns:
                cursor.execute(f"ALTER TABLE food_logs ADD COLUMN {col_name} {col_type}")
            
        conn.commit()
    except Exception as e:
        st.error(f"데이터베이스 구조 검사 중 복구 가능한 지연 오류가 발생했습니다. 새로고침을 해주세요: {e}")
    finally:
        if conn:
            conn.close()

# Streamlit의 캐시 데코레이터를 사용하여 서버 구동 시 딱 한 번만 데이터베이스 초기화를 진행합니다.
# 이를 통해 새로고침이나 여러 탭이 열릴 때 발생하는 database is locked 오류를 완벽하게 예방합니다.
@st.cache_resource
def run_database_initialization():
    init_db()

def hash_password(password):
    """비밀번호를 안전하게 보관하기 위해 SHA-256 방식으로 해싱(암호화)합니다."""
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, password):
    """새로운 사용자를 등록(회원가입)합니다."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        hashed = hash_password(password)
        cursor.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def login_user(username, password):
    """입력된 계정 정보를 조회하여 일치 여부(로그인 성공)를 확인합니다."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        hashed = hash_password(password)
        cursor.execute("SELECT * FROM users WHERE username = ? AND password = ?", (username, hashed))
        user = cursor.fetchone()
        return user is not None
    finally:
        conn.close()

# ------------------------------------------------------------------
# 2. 식단 로그 관리 함수 (사용자 격리 적용 및 안전한 트랜잭션 종료)
# ------------------------------------------------------------------
def add_food_log(username, food_name, calories, macros_json, image_bytes):
    """현재 로그인한 사용자의 고유 데이터로 식단과 사진을 기록합니다."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''
            INSERT INTO food_logs (username, timestamp, food_name, calories, macros_json, image)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (username, timestamp, food_name, calories, macros_json, image_bytes))
        conn.commit()
    finally:
        conn.close()

def get_logs_by_date(username, date_str):
    """특정 날짜에 해당하는 현재 사용자의 식단 목록을 조회합니다."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM food_logs 
            WHERE username = ? AND timestamp LIKE ? 
            ORDER BY timestamp DESC
        ''', (username, f"{date_str}%"))
        logs = cursor.fetchall()
        return logs
    finally:
        conn.close()

def delete_log(log_id, username):
    """지정한 식단 데이터를 안전하게 데이터베이스에서 영구 삭제합니다."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM food_logs WHERE id = ? AND username = ?", (log_id, username))
        conn.commit()
    finally:
        conn.close()

# ------------------------------------------------------------------
# 3. Gemini API 기반 멀티모달 음식 분석 함수
# ------------------------------------------------------------------
def analyze_food_image(image, api_key):
    """Google Gemini 2.5 Flash API를 활용하여 업로드한 이미지 속 음식을 식별하고 칼로리를 추정합니다."""
    try:
        client = genai.Client(api_key=api_key)
        
        prompt = """
        이 음식 사진을 꼼꼼하게 분석하고 칼로리와 탄수화물, 단백질, 지방 영양소 성분을 합리적으로 추정해 줘.
        응답은 반드시 마크다운 코드 블록이나 기타 텍스트 없이 오로지 아래 형식의 순수한 JSON 데이터만 전송해 줘.

        {
            "food_name": "식사 이름 또는 식별된 메인 음식 이름",
            "calories": 520,
            "carbs": 65,
            "protein": 22,
            "fat": 18,
            "comment": "이 식사의 영양 구성 평가와 건강을 위한 다정한 피드백 한마디"
        }
        """
        
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=85)
        pil_image = Image.open(img_byte_arr)
        
        config = types.GenerateContentConfig(
            response_mime_type="application/json"
        )
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[pil_image, prompt],
            config=config
        )
        
        result = json.loads(response.text)
        return result
    except Exception as e:
        st.error(f"AI 분석 중 에러가 발생했습니다: {e}")
        return None

# ------------------------------------------------------------------
# 4. Streamlit 웹 애플리케이션 화면 구성 및 초기화
# ------------------------------------------------------------------
# 데이터베이스 초기화 및 구조 복구 실행 (단 1회만 구동되게 안전 처리)
run_database_initialization()

# 페이지 설정
st.set_page_config(page_title="AI 식단 일지", page_icon="🥗", layout="centered")

# 세션 상태 변수 초기화
if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False
if "username" not in st.session_state:
    st.session_state["username"] = ""
if "logout_requested" not in st.session_state:
    st.session_state["logout_requested"] = False
if "auto_login_checked" not in st.session_state:
    st.session_state["auto_login_checked"] = False
if "pending_auto_login" not in st.session_state:
    st.session_state["pending_auto_login"] = None

# --- 자동 로그인 복구 백그라운드 프로세스 ---
query_params = st.query_params
if not st.session_state["logged_in"]:
    # 1) 로그아웃 버튼을 눌러 스토리지 삭제 예약이 걸린 상황인 경우
    if st.session_state["logout_requested"]:
        clear_login_from_local_storage()
        st.session_state["logout_requested"] = False
        st.rerun()
    # 2) 주소창에 자동 로그인 요청 파라미터가 유입된 경우
    elif "autouser" in query_params and "autotoken" in query_params:
        autouser = query_params["autouser"]
        autotoken = query_params["autotoken"]
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions WHERE username = ? AND token = ?", (autouser, autotoken))
            session_exists = cursor.fetchone()
            
            if session_exists:
                # 유효한 세션일 경우 로그인 세션 활성화
                st.session_state["logged_in"] = True
                st.session_state["username"] = autouser
                # 깔끔하게 주소창 쿼리스트링 초기화 및 새로고침
                st.query_params.clear()
                st.rerun()
            else:
                # 무효한 토큰일 시 루프 방지를 위해 체크 완료 상태로 돌리고 주소창 클리어
                st.session_state["auto_login_checked"] = True
                st.query_params.clear()
                st.rerun()
        except:
            st.query_params.clear()
        finally:
            conn.close()
    # 3) 로그인 스크린이 표시될 때, 스토리지 조회 동작이 진행되지 않은 상태라면 JS 조회 트리거 작동
    else:
        if not st.session_state["auto_login_checked"]:
            trigger_auto_login_check()
            st.session_state["auto_login_checked"] = True

# 새로운 자동 로그인 자격증명 저장 대기열 실행
if st.session_state["pending_auto_login"]:
    p_user, p_token = st.session_state["pending_auto_login"]
    save_login_to_local_storage(p_user, p_token)
    st.session_state["pending_auto_login"] = None

# 고급스러운 앱 스타일 및 고시인성 영양 분석 박스 CSS 반영 (다크 모드에서도 완벽한 시인성 보장)
st.markdown("""
<style>
    .main-header {
        font-family: 'Helvetica Neue', Arial, sans-serif;
        font-weight: 700;
        color: #2E7D32;
        text-align: center;
        margin-bottom: 25px;
    }
    .food-card {
        background-color: #f9f9f9;
        padding: 15px;
        border-radius: 10px;
        border-left: 5px solid #2E7D32;
        margin-bottom: 15px;
    }
    /* 영양성분 정보 박스 공통 디자인 및 모바일 간격 확보 */
    .metric-box {
        padding: 12px;
        border-radius: 8px;
        text-align: center;
        font-weight: bold;
        margin-bottom: 12px; /* 카드간 간격 확보 */
        box-shadow: 0 2px 5px rgba(0,0,0,0.05);
    }
    /* 각 영양성분 고유 색상 및 완벽한 글자 대비 설정 (!important로 강제 지정) */
    .metric-calories {
        background-color: #FFE082 !important; /* 부드러운 노란색 */
        color: #5D4037 !important;            /* 어두운 갈색 글자 */
    }
    .metric-carbs {
        background-color: #E3F2FD !important;    /* 부드러운 파란색 */
        color: #0D47A1 !important;               /* 어두운 파란색 글자 */
    }
    .metric-protein {
        background-color: #FCE4EC !important;  /* 부드러운 분홍색 */
        color: #880E4F !important;             /* 어두운 자주색 글자 */
    }
    .metric-fat {
        background-color: #FFF3E0 !important;      /* 부드러운 주황색 */
        color: #E65100 !important;                 /* 어두운 주황색 글자 */
    }
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------
# 로그인 / 회원 가입 화면 (비로그인 상태일 때)
# ------------------------------------------------------------------
if not st.session_state["logged_in"]:
    st.markdown("<h1 class='main-header'>🥗 AI 식단 일지</h1>", unsafe_allow_html=True)
    st.subheader("나만의 맞춤형 스마트 식단 일지")
    
    # 탭으로 깔끔하게 화면 분류
    tab1, tab2 = st.tabs(["🔐 로그인", "📝 회원 가입"])
    
    with tab1:
        st.write("계정에 로그인하여 식단을 기록해 보세요.")
        login_id = st.text_input("아이디 입력", key="login_id")
        login_pw = st.text_input("비밀번호 입력", type="password", key="login_pw")
        
        # 자동 로그인 유지 옵션 추가
        remember_me = st.checkbox("자동 로그인", value=True)
        
        if st.button("로그인하기", use_container_width=True):
            if login_id.strip() == "" or login_pw.strip() == "":
                st.warning("아이디와 비밀번호를 모두 입력해 주세요.")
            else:
                if login_user(login_id, login_pw):
                    st.session_state["logged_in"] = True
                    st.session_state["username"] = login_id
                    
                    # 자동 로그인을 희망하는 경우
                    if remember_me:
                        # 무작위 고유 보안 세션 토큰 생성
                        session_token = secrets.token_hex(24)
                        try:
                            conn = get_db_connection()
                            cursor = conn.cursor()
                            # 기존에 있던 동일 유저 세션은 깔끔하게 지우고 새로 적재
                            cursor.execute("DELETE FROM sessions WHERE username = ?", (login_id,))
                            cursor.execute("INSERT INTO sessions (username, token, created_at) VALUES (?, ?, ?)", 
                                           (login_id, session_token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                            conn.commit()
                            conn.close()
                            
                            # 로컬스토리지에 전달할 세션값 상태 예약
                            st.session_state["pending_auto_login"] = (login_id, session_token)
                        except Exception as e:
                            pass
                            
                    st.success(f"🎉 {login_id}님, 환영합니다! 로그인에 성공했습니다.")
                    st.rerun()
                else:
                    st.error("❌ 아이디 또는 비밀번호가 잘못되었습니다. 다시 입력해 주세요.")
                    
    with tab2:
        st.write("새로운 건강 여정을 위한 계정을 만들어 보세요.")
        reg_id = st.text_input("새로운 아이디 생성", key="reg_id")
        reg_pw = st.text_input("사용할 비밀번호 설정", type="password", key="reg_pw")
        reg_pw_confirm = st.text_input("비밀번호 확인", type="password", key="reg_pw_confirm")
        
        if st.button("회원 가입 완료하기", use_container_width=True):
            if reg_id.strip() == "" or reg_pw.strip() == "":
                st.warning("아이디와 비밀번호를 모두 적어주세요.")
            elif reg_pw != reg_pw_confirm:
                st.error("❌ 두 비밀번호가 서로 일치하지 않습니다.")
            else:
                if register_user(reg_id, reg_pw):
                    st.success("✅ 회원가입이 안전하게 완료되었습니다! 로그인 탭에서 이용해 주세요.")
                else:
                    st.error("❌ 이미 존재하는 아이디입니다. 다른 아이디를 입력해 주세요.")

# ------------------------------------------------------------------
# 메인 식단 기록 화면 (로그인 완료 상태일 때)
# ------------------------------------------------------------------
else:
    # 1) 사이드바 구성 (회원 정보 / 로그아웃)
    with st.sidebar:
        st.markdown(f"### 👤 **{st.session_state['username']}**님")
        st.write("오늘도 활기차고 건강한 하루 보내세요!")
        
        st.markdown("---")
        if st.button("로그아웃", use_container_width=True):
            # 로그아웃 시 DB의 활성 토큰 파기
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM sessions WHERE username = ?", (st.session_state["username"],))
                conn.commit()
                conn.close()
            except:
                pass
                
            st.session_state["logged_in"] = False
            st.session_state["username"] = ""
            st.session_state["logout_requested"] = True
            st.session_state["auto_login_checked"] = False  # 로그인 화면 재진입 시 복구 루프 복원
            st.success("로그아웃 되었습니다.")
            st.rerun()

    # 메인 헤더
    st.markdown("<h1 class='main-header'>🥗 AI 식단 일지</h1>", unsafe_allow_html=True)
    
    # 2) 메인 기능 분할 탭 (새 식단 등록 / 내 캘린더 조회)
    menu_tab1, menu_tab2 = st.tabs(["📸 새로운 식단 올리기", "📅 캘린더 일지 조회"])
    
    # ------------------
    # 탭 1: 음식 촬영 및 자동 분석
    # ------------------
    with menu_tab1:
        st.markdown("### 음식의 사진을 등록해 주세요")
        uploaded_file = st.file_uploader("카메라 아이콘을 눌러 사진을 찍거나 불러옵니다.", type=["jpg", "jpeg", "png"])
        
        if uploaded_file is not None:
            image = Image.open(uploaded_file)
            st.image(image, caption="업로드된 음식 이미지", use_container_width=True)
            
            if st.button("AI 자동 영양 분석 시작", type="primary", use_container_width=True):
                active_api_key = GEMINI_API_KEY if GEMINI_API_KEY != "YOUR_GEMINI_API_KEY_HERE" else os.environ.get("GEMINI_API_KEY", "")
                
                if not active_api_key or active_api_key.strip() == "":
                    st.error("⚠️ `main.py` 파일 상단의 `GEMINI_API_KEY` 변수에 실제 발급받은 Gemini API 키를 채워 넣어 주세요!")
                else:
                    with st.spinner("AI가 음식 이미지를 기반으로 칼로리를 추정 중입니다..."):
                        ai_result = analyze_food_image(image, active_api_key)
                        
                        if ai_result:
                            st.success("⚡ 분석이 성공적으로 마무리되었습니다!")
                            
                            food_name = ai_result.get("food_name", "미확인 음식")
                            calories = ai_result.get("calories", 0)
                            comment = ai_result.get("comment", "")
                            
                            macros = {
                                "carb": ai_result.get("carbs", 0),
                                "protein": ai_result.get("protein", 0),
                                "fat": ai_result.get("fat", 0)
                            }
                            
                            st.markdown(f"### 🍽️ 분석 결과: **{food_name}**")
                            
                            col1, col2, col3, col4 = st.columns(4)
                            with col1:
                                st.markdown(f"<div class='metric-box metric-calories'>🔥 {calories} kcal</div>", unsafe_allow_html=True)
                            with col2:
                                st.markdown(f"<div class='metric-box metric-carbs'>🍞 탄수화물: {macros['carb']}g</div>", unsafe_allow_html=True)
                            with col3:
                                st.markdown(f"<div class='metric-box metric-protein'>🍗 단백질: {macros['protein']}g</div>", unsafe_allow_html=True)
                            with col4:
                                st.markdown(f"<div class='metric-box metric-fat'>🥑 지방: {macros['fat']}g</div>", unsafe_allow_html=True)
                                
                            st.info(f"💡 **AI 한줄평**: {comment}")
                            
                            img_byte_arr = io.BytesIO()
                            image.save(img_byte_arr, format='JPEG')
                            binary_data = img_byte_arr.getvalue()
                            
                            macros_str = json.dumps(macros)
                            add_food_log(
                                username=st.session_state["username"],
                                food_name=food_name,
                                calories=calories,
                                macros_json=macros_str,
                                image_bytes=binary_data
                            )
                            st.success("💾 분석 결과를 내 캘린더 다이어리에 안전하게 기록했습니다!")

    # ------------------
    # 탭 2: 캘린더를 이용한 이력 추적 및 모니터링
    # ------------------
    with menu_tab2:
        st.markdown("### 📅 일자별 식단 다이어리")
        selected_date = st.date_input("조회하고 기록을 관리할 날짜를 골라주세요", datetime.today())
        selected_date_str = selected_date.strftime("%Y-%m-%d")
        
        daily_logs = get_logs_by_date(st.session_state["username"], selected_date_str)
        
        if not daily_logs:
            st.info(f"아직 {selected_date_str}에 등록된 식단 로그가 없습니다. 새로운 음식 사진을 등록해 보세요!")
        else:
            total_calories = 0
            total_carbs = 0
            total_protein = 0
            total_fat = 0
            
            for log in daily_logs:
                total_calories += log["calories"]
                try:
                    macros = json.loads(log["macros_json"])
                    total_carbs += macros.get("carb", 0)
                    total_protein += macros.get("protein", 0)
                    total_fat += macros.get("fat", 0)
                except:
                    pass
            
            st.markdown(f"#### 📊 {selected_date_str} 일일 섭취 총합")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("🔥 총 칼로리", f"{total_calories} kcal")
            col2.metric("🍞 총 탄수화물", f"{total_carbs} g")
            col3.metric("🍗 총 단백질", f"{total_protein} g")
            col4.metric("🥑 총 지방", f"{total_fat} g")
            st.markdown("---")
            
            st.markdown("#### 🕒 오늘의 기록 리스트")
            for log in daily_logs:
                log_id = log["id"]
                log_time = datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S").strftime("%p %I시 %M분")
                
                c_img, c_info, c_action = st.columns([1.5, 3, 1])
                
                with c_img:
                    if log["image"]:
                        try:
                            log_image = Image.open(io.BytesIO(log["image"]))
                            st.image(log_image, use_container_width=True)
                        except:
                            st.write("📷 사진 없음")
                    else:
                        st.write("📷 사진 없음")
                
                with c_info:
                    st.markdown(f"**{log['food_name']}**")
                    st.write(f"⏱️ 등록 시각: {log_time}")
                    st.write(f"🔥 칼로리: **{log['calories']} kcal**")
                    try:
                        macros = json.loads(log["macros_json"])
                        st.write(f"💡 탄: {macros.get('carb', 0)}g / 단: {macros.get('protein', 0)}g / 지: {macros.get('fat', 0)}g")
                    except:
                        pass
                
                with c_action:
                    st.write("")
                    if st.button("❌ 삭제", key=f"del_{log_id}", use_container_width=True):
                        delete_log(log_id, st.session_state["username"])
                        st.success("식단이 삭제되었습니다.")
                        st.rerun()
                
                st.markdown("<div style='border-bottom:1px solid #f0f0f0; margin-bottom:15px;'></div>", unsafe_allow_html=True)
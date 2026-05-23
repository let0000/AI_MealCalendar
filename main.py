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
# [보안] 안전한 API Key 획득 함수 (무조건 파일/배포 Secrets에서만 조회)
# ------------------------------------------------------------------
def get_safe_api_key():
    """무조건 Streamlit Secrets(.streamlit/secrets.toml 또는 배포 서버 설정) 공간에서만 키를 조회합니다."""
    if "GEMINI_API_KEY" in st.secrets:
        return st.secrets["GEMINI_API_KEY"]
    return ""

# ------------------------------------------------------------------
# 자동 로그인 관련 브라우저 Storage JS 헬퍼 함수 (표준형)
# ------------------------------------------------------------------
def save_login_to_local_storage(username, token):
    js_code = f"""
    <script>
        localStorage.setItem('auto_user', '{username}');
        localStorage.setItem('auto_token', '{token}');
    </script>
    """
    st.components.v1.html(js_code, height=0)

def clear_login_from_local_storage():
    js_code = """
    <script>
        localStorage.removeItem('auto_user');
        localStorage.removeItem('auto_token');
    </script>
    """
    st.components.v1.html(js_code, height=0)

def trigger_auto_login_check():
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
    conn = sqlite3.connect(DB_NAME, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL
            )
        ''')
        
        cursor.execute("PRAGMA table_info(food_logs)")
        existing_columns = [col[1] for col in cursor.fetchall()]
        
        if existing_columns and ('date' in existing_columns or 'timestamp' not in existing_columns):
            cursor.execute("DROP TABLE IF EXISTS food_logs")
            existing_columns = []
            
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
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                username TEXT,
                token TEXT PRIMARY KEY,
                created_at TEXT
            )
        ''')
        
        cursor.execute("PRAGMA table_info(food_logs)")
        existing_columns = [col[1] for col in cursor.fetchall()]
        
        required_columns = {
            "username": "TEXT DEFAULT 'guest'",
            "timestamp": "TEXT",
            "food_name": "TEXT",
            "calories": "INTEGER",
            "macros_json": "TEXT",
            "image": "BLOB"
        }
        
        for col_name, col_type in required_columns.items():
            if col_name not in existing_columns:
                cursor.execute(f"ALTER TABLE food_logs ADD COLUMN {col_name} {col_type}")
            
        conn.commit()
    except Exception as e:
        st.error(f"데이터베이스 스키마 복구 중 오류 발생: {e}")
    finally:
        if conn:
            conn.close()

@st.cache_resource
def run_database_initialization():
    init_db()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def register_user(username, password):
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
# 2. 식단 로그 관리 함수
# ------------------------------------------------------------------
def get_logs_by_date(username, date_str):
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
run_database_initialization()

st.set_page_config(page_title="AI 식단 일지", page_icon="🥗", layout="centered")

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

# --- 자동 로그인 복구 프로세스 ---
query_params = st.query_params
if not st.session_state["logged_in"]:
    if st.session_state["logout_requested"]:
        clear_login_from_local_storage()
        st.session_state["logout_requested"] = False
        st.rerun()
    elif "autouser" in query_params and "autotoken" in query_params:
        autouser = query_params["autouser"]
        autotoken = query_params["autotoken"]
        
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions WHERE username = ? AND token = ?", (autouser, autotoken))
            session_exists = cursor.fetchone()
            
            if session_exists:
                st.session_state["logged_in"] = True
                st.session_state["username"] = autouser
                st.query_params.clear()
                st.rerun()
            else:
                st.session_state["auto_login_checked"] = True
                st.query_params.clear()
                st.rerun()
        except:
            st.query_params.clear()
        finally:
            conn.close()
    else:
        if not st.session_state["auto_login_checked"]:
            trigger_auto_login_check()
            st.session_state["auto_login_checked"] = True

if st.session_state["pending_auto_login"]:
    p_user, p_token = st.session_state["pending_auto_login"]
    save_login_to_local_storage(p_user, p_token)
    st.session_state["pending_auto_login"] = None

st.markdown("""
<style>
    .main-header {
        font-family: 'Helvetica Neue', Arial, sans-serif;
        font-weight: 700;
        color: #2E7D32;
        text-align: center;
        margin-bottom: 25px;
    }
    .metric-box {
        padding: 12px;
        border-radius: 8px;
        text-align: center;
        font-weight: bold;
        margin-bottom: 12px;
        box-shadow: 0 2px 5px rgba(0,0,0,0.05);
    }
    .metric-calories { background-color: #FFE082 !important; color: #5D4037 !important; }
    .metric-carbs { background-color: #E3F2FD !important; color: #0D47A1 !important; }
    .metric-protein { background-color: #FCE4EC !important; color: #880E4F !important; }
    .metric-fat { background-color: #FFF3E0 !important; color: #E65100 !important; }
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------
# 비로그인 화면 (로그인 / 회원 가입)
# ------------------------------------------------------------------
if not st.session_state["logged_in"]:
    st.markdown("<h1 class='main-header'>🥗 AI 식단 일지</h1>", unsafe_allow_html=True)
    st.subheader("나만의 맞춤형 스마트 식단 일지")
    
    tab1, tab2 = st.tabs(["🔐 로그인", "📝 회원 가입"])
    
    with tab1:
        st.write("계정에 로그인하여 식단을 기록해 보세요.")
        login_id = st.text_input("아이디 입력", key="login_id")
        login_pw = st.text_input("비밀번호 입력", type="password", key="login_pw")
        remember_me = st.checkbox("자동 로그인 상태 유지", value=True)
        
        if st.button("로그인하기", use_container_width=True):
            if login_id.strip() == "" or login_pw.strip() == "":
                st.warning("아이디와 비밀번호를 모두 입력해 주세요.")
            else:
                if login_user(login_id, login_pw):
                    st.session_state["logged_in"] = True
                    st.session_state["username"] = login_id
                    
                    if remember_me:
                        session_token = secrets.token_hex(24)
                        try:
                            conn = get_db_connection()
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM sessions WHERE username = ?", (login_id,))
                            cursor.execute("INSERT INTO sessions (username, token, created_at) VALUES (?, ?, ?)", 
                                           (login_id, session_token, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                            conn.commit()
                            conn.close()
                            st.session_state["pending_auto_login"] = (login_id, session_token)
                        except:
                            pass
                            
                    st.success(f"🎉 {login_id}님, 환영합니다!")
                    st.rerun()
                else:
                    st.error("❌ 아이디 또는 비밀번호가 잘못되었습니다.")
                    
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
                    st.success("✅ 회원가입이 완료되었습니다! 로그인 탭을 이용해 주세요.")
                else:
                    st.error("❌ 이미 존재하는 아이디입니다.")

# ------------------------------------------------------------------
# 메인 서비스 대시보드 (로그인 상태)
# ------------------------------------------------------------------
else:
    with st.sidebar:
        st.markdown(f"### 👤 **{st.session_state['username']}**님")
        st.write("오늘도 건강한 하루 보내세요!")
        st.markdown("---")
        if st.button("로그아웃", use_container_width=True):
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
            st.session_state["auto_login_checked"] = False
            st.success("로그아웃 되었습니다.")
            st.rerun()

    st.markdown("<h1 class='main-header'>🥗 AI 식단 일지</h1>", unsafe_allow_html=True)
    menu_tab1, menu_tab2 = st.tabs(["📸 새로운 식단 올리기", "📅 캘린더 일지 조회"])
    
    # --------------------------------------------------------------
    # 탭 1: 음식 촬영 ➡️ AI 분석 ➡️ 검토 및 시간 보정 후 선택 저장
    # --------------------------------------------------------------
    with menu_tab1:
        st.markdown("### 음식의 사진을 등록해 주세요")
        uploaded_file = st.file_uploader("카메라로 사진을 찍거나 갤러리에서 불러옵니다.", type=["jpg", "jpeg", "png"])
        
        if uploaded_file is not None:
            image = Image.open(uploaded_file)
            st.image(image, caption="업로드된 음식 이미지", use_container_width=True)
            
            if st.button("AI 자동 영양 분석 시작", type="primary", use_container_width=True):
                active_api_key = get_safe_api_key()
                
                if not active_api_key or active_api_key.strip() == "":
                    st.error("⚠️ Gemini API 키를 찾을 수 없습니다. Secrets 공간 설정을 확인해 주세요.")
                else:
                    with st.spinner("AI가 음식 성분을 정밀 분석 중입니다..."):
                        ai_result = analyze_food_image(image, active_api_key)
                        if ai_result:
                            st.success("⚡ 분석이 성공적으로 마무리되었습니다!")
                            st.session_state["current_ai_result"] = ai_result
                            st.session_state["current_image"] = image
                            
            if "current_ai_result" in st.session_state and st.session_state["current_ai_result"]:
                result = st.session_state["current_ai_result"]
                food_name = result.get("food_name", "미확인 음식")
                calories = result.get("calories", 0)
                comment = result.get("comment", "")
                macros = {
                    "carb": result.get("carbs", 0),
                    "protein": result.get("protein", 0),
                    "fat": result.get("fat", 0)
                }
                
                st.markdown("---")
                st.markdown(f"### 🍽️ AI 분석 결과 요약: **{food_name}**")
                
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
                
                st.markdown("### 💾 기록 세부 정보 및 저장 선택")
                
                # 🌟 [버그 수정 핵심] st.form 내부 위젯이 리턴하는 값을 완전히 고정하기 위해 key 값을 명시적으로 부여합니다.
                with st.form("save_log_form", clear_on_submit=False):
                    st.write("실제 식사 정보 및 시간과 일치하는지 확인하고 아래 저장 버튼을 눌러주세요.")
                    
                    # 폼이 그려지는 기준 시점의 시간을 들고 옵니다.
                    base_now = datetime.now()
                    
                    edit_date = st.date_input("식사 날짜 선택", value=base_now.date(), key="input_edit_date")
                    edit_time = st.time_input("식사 시간 선택", value=base_now.time(), key="input_edit_time")
                    
                    final_food_name = st.text_input("음식명 최종 보정", value=food_name, key="input_food_name")
                    final_calories = st.number_input("칼로리 최종 보정 (kcal)", value=int(calories), step=1, key="input_calories")
                    
                    submit_save = st.form_submit_button("📝 이 내용으로 일지에 저장하기", type="primary", use_container_width=True)
                    
                    if submit_save:
                        # 🌟 [버그 수정 핵심 2] 버튼 클릭 시점 재렌더링으로 위젯 변수가 유실되는 것을 막기 위해 st.session_state 가 물고 있는 위젯의 실시간 state 값을 직접 바인딩합니다.
                        chosen_date = st.session_state["input_edit_date"]
                        chosen_time = st.session_state["input_edit_time"]
                        chosen_name = st.session_state["input_food_name"]
                        chosen_calories = st.session_state["input_calories"]
                        
                        combined_datetime = datetime.combine(chosen_date, chosen_time)
                        formatted_timestamp = combined_datetime.strftime("%Y-%m-%d %H:%M:%S")
                        
                        img_byte_arr = io.BytesIO()
                        st.session_state["current_image"].save(img_byte_arr, format='JPEG')
                        binary_data = img_byte_arr.getvalue()
                        macros_str = json.dumps(macros)
                        
                        conn = get_db_connection()
                        try:
                            cursor = conn.cursor()
                            cursor.execute('''
                                INSERT INTO food_logs (username, timestamp, food_name, calories, macros_json, image)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (st.session_state["username"], formatted_timestamp, chosen_name, chosen_calories, macros_str, binary_data))
                            conn.commit()
                            st.success(f"💾 {formatted_timestamp} 일자로 식단 일지에 안전하게 기록되었습니다!")
                            
                            del st.session_state["current_ai_result"]
                            del st.session_state["current_image"]
                            st.rerun()
                        except Exception as e:
                            st.error(f"저장 중 처리 에러가 발생했습니다: {e}")
                        finally:
                            conn.close()

    # --------------------------------------------------------------
    # 탭 2: 캘린더를 이용한 이력 추적 및 모니터링
    # --------------------------------------------------------------
    with menu_tab2:
        st.markdown("### 📅 일자별 식단 다이어리")
        selected_date = st.date_input("조회하고 기록을 관리할 날짜를 골라주세요", datetime.today())
        selected_date_str = selected_date.strftime("%Y-%m-%d")
        
        daily_logs = get_logs_by_date(st.session_state["username"], selected_date_str)
        
        if not daily_logs:
            st.info(f"아직 {selected_date_str}에 등록된 식단 로그가 없습니다.")
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
                meal_time = datetime.strptime(log["timestamp"], "%Y-%m-%d %H:%M:%S").strftime("%p %I시 %M분")
                
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
                    st.write(f"⏱️ 식사 시각: {meal_time}")
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
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from functools import wraps
from sqlalchemy import func
import os, io, secrets
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PyPDF2 import PdfReader, PdfWriter
# ── ✅ 변경 1: Cloudinary 추가 ──
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
db = SQLAlchemy(app)
os.makedirs('uploads', exist_ok=True)

# ── ✅ 변경 2: Cloudinary 설정 추가 ──
cloudinary.config(
    cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME', 'dspi76gpo'),
    api_key    = os.environ.get('CLOUDINARY_API_KEY', '129521586826764'),
    api_secret = os.environ.get('CLOUDINARY_API_SECRET', 'y080TBqpcuuOLb3thTzbGISFE8Q')
)

pdfmetrics.registerFont(TTFont('NanumGothic', 'NanumGothic.ttf'))

# ──────────────────────────────
# 모델
# ──────────────────────────────
class User(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    username        = db.Column(db.String(50), unique=True, nullable=False)
    password        = db.Column(db.String(200), nullable=False)
    is_admin        = db.Column(db.Boolean, default=False)
    is_banned       = db.Column(db.Boolean, default=False)
    ban_type        = db.Column(db.String(20), default=None)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    login_attempts  = db.Column(db.Integer, default=0)
    locked_until    = db.Column(db.DateTime, nullable=True)

class PDF(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    filename    = db.Column(db.String(300), nullable=False)
    grade       = db.Column(db.String(50), nullable=False)
    category    = db.Column(db.String(50), nullable=False)
    uploader_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    likes        = db.relationship('Like', backref='pdf', lazy=True, cascade='all, delete-orphan')
    pdf_comments = db.relationship('PdfComment', backref='pdf', lazy=True, cascade='all, delete-orphan')

class Like(db.Model):
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    pdf_id  = db.Column(db.Integer, db.ForeignKey('pdf.id'))

class PdfComment(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    content    = db.Column(db.String(500), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'))
    pdf_id     = db.Column(db.Integer, db.ForeignKey('pdf.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user       = db.relationship('User', backref='pdf_comments')

class Post(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    title         = db.Column(db.String(200), nullable=False)
    content       = db.Column(db.Text, nullable=False)
    author_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author        = db.relationship('User', backref='posts')
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    post_comments = db.relationship('PostComment', backref='post', lazy=True, cascade='all, delete-orphan')

class PostComment(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    content    = db.Column(db.Text, nullable=False)
    author_id  = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    author     = db.relationship('User', backref='post_comments')
    post_id    = db.Column(db.Integer, db.ForeignKey('post.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class VisitLog(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    username   = db.Column(db.String(50), default='비로그인')
    ip         = db.Column(db.String(50), nullable=False)
    page       = db.Column(db.String(200), nullable=False)
    visited_at = db.Column(db.DateTime, default=datetime.utcnow)
    user       = db.relationship('User', backref='visit_logs')

class LoginFailLog(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    username  = db.Column(db.String(50), nullable=False)
    ip        = db.Column(db.String(50), nullable=False)
    reason    = db.Column(db.String(100), nullable=False)
    failed_at = db.Column(db.DateTime, default=datetime.utcnow)

# ──────────────────────────────
# DB 초기화
# ──────────────────────────────
with app.app_context():
    db.create_all()
    if not User.query.filter_by(username='admin').first():
        admin_pw = os.environ.get('ADMIN_PASSWORD', secrets.token_urlsafe(16))
        admin = User(
            username='admin',
            password=generate_password_hash(admin_pw),
            is_admin=True
        )
        db.session.add(admin)
        db.session.commit()
        print(f'관리자 계정 생성 완료 / 비밀번호: {admin_pw}')

# ──────────────────────────────
# 방문 기록 자동 저장
# ──────────────────────────────
EXCLUDE_PATHS = ['/static', '/favicon.ico', '/admin/visits', '/admin/loginfails']
@app.before_request
def log_visit():
    for path in EXCLUDE_PATHS:
        if request.path.startswith(path):
            return
    user_id  = session.get('user_id')
    username = session.get('username', '비로그인')
    ip       = request.remote_addr
    page     = request.path
    log = VisitLog(user_id=user_id, username=username, ip=ip, page=page)
    db.session.add(log)
    db.session.commit()

# ──────────────────────────────
# 데코레이터
# ──────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('로그인을 하신 후 이용해 주십시오!', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

ADMIN_IPS = ['127.0.0.1', '::1', '121.165.139.150']
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        client_ip = request.remote_addr
        if client_ip not in ADMIN_IPS:
            return render_template('403.html', ip=client_ip), 403
        if not session.get('is_admin'):
            flash('관리자만 접근할 수 있습니다.', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

# ──────────────────────────────
# PDF 워터마크 함수
# ──────────────────────────────
def add_watermark(input_path, username, unique_id):
    reader = PdfReader(input_path)
    writer = PdfWriter()
    for page in reader.pages:
        w = float(page.mediabox.width)
        h = float(page.mediabox.height)
        packet = io.BytesIO()
        c = canvas.Canvas(packet, pagesize=(w, h))
        c.setFont("NanumGothic", 7)
        c.setFillColorRGB(0.5, 0.5, 0.5, alpha=0.5)
        c.drawString(10, 10, f"생각하는황소자료실 | {username} | ID:{unique_id}")
        c.save()
        packet.seek(0)
        watermark_pdf  = PdfReader(packet)
        watermark_page = watermark_pdf.pages[0]
        page.merge_page(watermark_page)
        writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
    return output

# ──────────────────────────────
# 상수
# ──────────────────────────────
GRADES = [
    '초등학교 4학년', '초등학교 5학년', '초등학교 6학년',
    '중학교 1학년', '중학교 2학년', '중학교 3학년',
    '고등학교 1학년'
]
CATEGORIES   = ['단원평가', '단원정리', '퀵테스트', '기타']
ALLOWED_MIME = {'application/pdf'}

# ──────────────────────────────
# 라우트 - 메인
# ──────────────────────────────
@app.route('/')
@login_required
def index():
    user     = User.query.get(session['user_id'])
    grade    = request.args.get('grade', '')
    category = request.args.get('category', '')
    query = PDF.query
    if grade:
        query = query.filter_by(grade=grade)
    if category:
        query = query.filter_by(category=category)
    pdfs      = query.order_by(PDF.uploaded_at.desc()).all()
    liked_ids = [l.pdf_id for l in Like.query.filter_by(user_id=user.id).all()]
    return render_template('index.html',
        user=user, pdfs=pdfs,
        grades=GRADES, categories=CATEGORIES,
        selected_grade=grade, selected_category=category,
        liked_ids=liked_ids
    )

# ──────────────────────────────
# 라우트 - 로그인
# ──────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        ip       = request.remote_addr
        user     = User.query.filter_by(username=username).first()
        if not user:
            db.session.add(LoginFailLog(username=username, ip=ip, reason='존재하지 않는 아이디'))
            db.session.commit()
            flash('아이디 또는 비밀번호가 올바르지 않습니다.', 'error')
        elif user.is_banned:
            ban_msg = '영구 정지' if user.ban_type == 'permanent' else '일시 정지'
            db.session.add(LoginFailLog(username=username, ip=ip, reason=f'정지된 계정 ({ban_msg})'))
            db.session.commit()
            flash(f'이용이 {ban_msg}된 계정입니다.', 'error')
        elif user.locked_until and datetime.utcnow() < user.locked_until:
            remaining = (user.locked_until - datetime.utcnow()).seconds // 60
            db.session.add(LoginFailLog(username=username, ip=ip, reason=f'잠금 상태 ({remaining}분 남음)'))
            db.session.commit()
            flash(f'로그인 시도가 너무 많습니다. {remaining}분 후 다시 시도해주세요.', 'error')
        elif not check_password_hash(user.password, password):
            user.login_attempts += 1
            reason = f'비밀번호 오류 ({user.login_attempts}/5)'
            if user.login_attempts >= 5:
                user.locked_until   = datetime.utcnow() + timedelta(minutes=15)
                user.login_attempts = 0
                reason = '비밀번호 5회 오류 → 잠금'
                flash('로그인 5회 실패. 15분간 잠금됩니다.', 'error')
            else:
                flash(f'아이디 또는 비밀번호가 올바르지 않습니다. ({user.login_attempts}/5)', 'error')
            db.session.add(LoginFailLog(username=username, ip=ip, reason=reason))
            db.session.commit()
        else:
            session.clear()
            user.login_attempts = 0
            user.locked_until   = None
            db.session.commit()
            session['user_id']  = user.id
            session['username'] = user.username
            session['is_admin'] = user.is_admin
            flash(f'{user.username}님, 환영합니다!', 'success')
            return redirect(url_for('index'))
    return render_template('login.html')

# ──────────────────────────────
# 라우트 - 회원가입
# ──────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        username         = request.form['username'].strip()
        password         = request.form['password']
        password_confirm = request.form['password_confirm']
        if len(username) < 3 or len(username) > 20:
            flash('아이디는 3~20자 이내여야 합니다.', 'error')
        elif not username.isalnum():
            flash('아이디는 영문자와 숫자만 사용 가능합니다.', 'error')
        elif User.query.filter_by(username=username).first():
            flash('이미 사용 중인 아이디입니다.', 'error')
        elif len(password) < 6 or len(password) > 50:
            flash('비밀번호는 6~50자 이내여야 합니다.', 'error')
        elif password != password_confirm:
            flash('비밀번호가 일치하지 않습니다.', 'error')
        else:
            new_user = User(username=username, password=generate_password_hash(password))
            db.session.add(new_user)
            db.session.commit()
            flash('회원가입 완료! 로그인해주세요.', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ──────────────────────────────
# 라우트 - PDF 업로드 (관리자)
# ── ✅ 변경 3: Cloudinary에 업로드 추가 ──
# ──────────────────────────────
@app.route('/upload', methods=['GET', 'POST'])
@login_required
@admin_required
def upload():
    if request.method == 'POST':
        title    = request.form['title'].strip()
        grade    = request.form['grade']
        category = request.form['category']
        file     = request.files['file']

        if grade not in GRADES or category not in CATEGORIES:
            flash('올바르지 않은 학년 또는 카테고리입니다.', 'error')
            return redirect(url_for('upload'))
        if not file or not file.filename.endswith('.pdf'):
            flash('PDF 파일만 업로드 가능합니다.', 'error')
            return redirect(url_for('upload'))

       
        file.seek(0)

        original_name = secure_filename(file.filename)
        unique_prefix = secrets.token_hex(8)
        filename      = f"{unique_prefix}_{original_name}"
        save_path     = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(save_path)

        # ✅ Cloudinary에도 업로드 (로컬 저장 후 추가 업로드)
        cloudinary.uploader.upload(
            save_path,
            public_id=filename,
            resource_type='raw',
            folder='황소자료실'
        )

        new_pdf = PDF(
            title=title,
            filename=filename,
            grade=grade,
            category=category,
            uploader_id=session['user_id']
        )
        db.session.add(new_pdf)
        db.session.commit()
        flash('PDF 업로드 완료!', 'success')
        return redirect(url_for('index'))
    return render_template('upload.html', grades=GRADES, categories=CATEGORIES)

# ──────────────────────────────
# 라우트 - PDF 삭제 (관리자)
# ──────────────────────────────
@app.route('/delete_pdf/<int:pdf_id>')
@login_required
@admin_required
def delete_pdf(pdf_id):
    pdf = PDF.query.get_or_404(pdf_id)
    # ✅ Cloudinary에서도 삭제
    try:
        cloudinary.uploader.destroy(f"황소자료실/{pdf.filename}", resource_type='raw')
    except:
        pass
    try:
        os.remove(os.path.join(app.config['UPLOAD_FOLDER'], pdf.filename))
    except:
        pass
    db.session.delete(pdf)
    db.session.commit()
    flash('PDF가 삭제되었습니다.', 'success')
    return redirect(url_for('index'))

# ──────────────────────────────
# 라우트 - PDF 다운로드 (워터마크 포함)
# ── ✅ 변경 4: 로컬 없으면 Cloudinary에서 다운로드 ──
# ──────────────────────────────
@app.route('/download/<int:pdf_id>')
@login_required
def download(pdf_id):
    pdf  = PDF.query.get_or_404(pdf_id)
    user = User.query.get(session['user_id'])
    safe_path = os.path.join(app.config['UPLOAD_FOLDER'], os.path.basename(pdf.filename))

    # ✅ 로컬에 없으면 Cloudinary에서 받아서 임시 저장
    if not os.path.exists(safe_path):
        try:
            import urllib.request
            url = cloudinary.utils.cloudinary_url(
                f"황소자료실/{pdf.filename}",
                resource_type='raw'
            )[0]
            urllib.request.urlretrieve(url, safe_path)
        except:
            flash('파일을 찾을 수 없습니다.', 'error')
            return redirect(url_for('index'))

    output = add_watermark(safe_path, user.username, f"{user.id}-{pdf.id}")
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{pdf.title}.pdf",
        mimetype='application/pdf'
    )

# ──────────────────────────────
# 라우트 - 좋아요
# ──────────────────────────────
@app.route('/like/<int:pdf_id>', methods=['POST'])
@login_required
def like(pdf_id):
    PDF.query.get_or_404(pdf_id)
    user_id  = session['user_id']
    existing = Like.query.filter_by(user_id=user_id, pdf_id=pdf_id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(Like(user_id=user_id, pdf_id=pdf_id))
    db.session.commit()
    count = Like.query.filter_by(pdf_id=pdf_id).count()
    return {'liked': existing is None, 'count': count}

# ──────────────────────────────
# 라우트 - PDF 댓글
# ──────────────────────────────
@app.route('/comment/<int:pdf_id>', methods=['POST'])
@login_required
def add_comment(pdf_id):
    content = request.form['content'].strip()
    if content and len(content) <= 300:
        db.session.add(PdfComment(
            content=content,
            user_id=session['user_id'],
            pdf_id=pdf_id
        ))
        db.session.commit()
    elif len(content) > 300:
        flash('댓글은 300자 이내로 작성해주세요.', 'error')
    return redirect(url_for('index') + f'#{pdf_id}')

@app.route('/delete_comment/<int:comment_id>')
@login_required
def delete_comment(comment_id):
    comment = PdfComment.query.get_or_404(comment_id)
    pdf_id  = comment.pdf_id
    if not session.get('is_admin') and comment.user_id != session['user_id']:
        flash('삭제 권한이 없습니다.', 'error')
        return redirect(url_for('index') + f'#{pdf_id}')
    db.session.delete(comment)
    db.session.commit()
    return redirect(url_for('index') + f'#{pdf_id}')

# ──────────────────────────────
# 라우트 - 건의 게시판
# ──────────────────────────────
@app.route('/board')
@login_required
def board():
    posts = Post.query.order_by(Post.created_at.desc()).all()
    return render_template('board.html', posts=posts)

@app.route('/board/write', methods=['GET', 'POST'])
@login_required
def write_post():
    if request.method == 'POST':
        title   = request.form['title'].strip()
        content = request.form['content'].strip()
        if not title or not content:
            flash('제목과 내용을 모두 입력해주세요.', 'error')
        elif len(title) > 100:
            flash('제목은 100자 이내로 작성해주세요.', 'error')
        elif len(content) > 2000:
            flash('내용은 2000자 이내로 작성해주세요.', 'error')
        else:
            post = Post(title=title, content=content, author_id=session['user_id'])
            db.session.add(post)
            db.session.commit()
            flash('게시글이 작성되었습니다.', 'success')
            return redirect(url_for('board'))
    return render_template('write_post.html')

@app.route('/board/<int:post_id>')
@login_required
def view_post(post_id):
    post = Post.query.get_or_404(post_id)
    return render_template('view_post.html', post=post)

@app.route('/board/delete/<int:post_id>')
@login_required
def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    if not session.get('is_admin') and post.author_id != session['user_id']:
        flash('삭제 권한이 없습니다.', 'error')
        return redirect(url_for('board'))
    db.session.delete(post)
    db.session.commit()
    flash('게시글이 삭제되었습니다.', 'success')
    return redirect(url_for('board'))

@app.route('/board/<int:post_id>/comment', methods=['POST'])
@login_required
def add_post_comment(post_id):
    content = request.form['content'].strip()
    if content and len(content) <= 300:
        db.session.add(PostComment(
            content=content,
            author_id=session['user_id'],
            post_id=post_id
        ))
        db.session.commit()
    elif len(content) > 300:
        flash('댓글은 300자 이내로 작성해주세요.', 'error')
    return redirect(url_for('view_post', post_id=post_id))

@app.route('/board/comment/delete/<int:comment_id>')
@login_required
def delete_post_comment(comment_id):
    comment = PostComment.query.get_or_404(comment_id)
    post_id = comment.post_id
    if not session.get('is_admin') and comment.author_id != session['user_id']:
        flash('삭제 권한이 없습니다.', 'error')
        return redirect(url_for('view_post', post_id=post_id))
    db.session.delete(comment)
    db.session.commit()
    flash('댓글이 삭제되었습니다.', 'success')
    return redirect(url_for('view_post', post_id=post_id))

# ──────────────────────────────
# 라우트 - 관리자
# ──────────────────────────────
@app.route('/admin')
@login_required
@admin_required
def admin():
    users = User.query.filter_by(is_admin=False).all()
    return render_template('admin.html', users=users)

@app.route('/admin/ban/<int:user_id>')
@login_required
@admin_required
def ban_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_banned = True
    user.ban_type  = 'temp'
    db.session.commit()
    flash(f'{user.username} 님을 일시 정지했습니다.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/permban/<int:user_id>')
@login_required
@admin_required
def permban_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_banned = True
    user.ban_type  = 'permanent'
    db.session.commit()
    flash(f'{user.username} 님을 영구 정지했습니다.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/unban/<int:user_id>')
@login_required
@admin_required
def unban_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_banned = False
    user.ban_type  = None
    db.session.commit()
    flash(f'{user.username} 님의 정지를 해제했습니다.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/visits')
@login_required
@admin_required
def visit_logs():
    logs       = VisitLog.query.order_by(VisitLog.visited_at.desc()).limit(200).all()
    total      = VisitLog.query.count()
    unique_ips = db.session.query(VisitLog.ip).distinct().count()
    return render_template('visits.html', logs=logs, total=total, unique_ips=unique_ips)

@app.route('/admin/loginfails')
@login_required
@admin_required
def login_fail_logs():
    logs     = LoginFailLog.query.order_by(LoginFailLog.failed_at.desc()).limit(200).all()
    total    = LoginFailLog.query.count()
    ip_stats = db.session.query(
        LoginFailLog.ip,
        func.count(LoginFailLog.id).label('count')
    ).group_by(LoginFailLog.ip).order_by(func.count(LoginFailLog.id).desc()).all()
    return render_template('login_fails.html',
        logs     = logs,
        total    = total,
        ip_stats = ip_stats
    )

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

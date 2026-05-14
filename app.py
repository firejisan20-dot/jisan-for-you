import os, sqlite3, zipfile, subprocess, signal, shutil, psutil, time, datetime, socket, re, threading
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit

# Global process tracker
running_procs = {}
start_times = {}

socketio = SocketIO()

# ============================================================
# PORT & URL HELPERS
# ============================================================

def get_free_port(start=6000, end=6999):
    for p in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('', p))
                return p
            except OSError:
                continue
    return None

def get_public_url(port, req=None):
    """
    CodeSandbox বা ngrok থেকে public URL বানাও।
    """
    # 1. CodeSandbox detect
    if req:
        host = req.host  # e.g. "5l3rlq-3006.csb.app"
        if '.csb.app' in host:
            parts = host.split('-')
            if len(parts) >= 2:
                sandbox_id = '-'.join(parts[:-1])
                return f"https://{sandbox_id}-{port}.csb.app"

    # 2. ngrok active tunnel check
    try:
        import urllib.request, json
        r = urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=1)
        data = json.loads(r.read())
        for t in data.get("tunnels", []):
            if str(port) in t.get("config", {}).get("addr", ""):
                return t["public_url"]
    except:
        pass

    # 3. fallback
    return f"http://localhost:{port}"

def inject_port(file_path, port):
    """
    User এর startup file এ PORT auto inject করো।
    app.run() খুঁজে replace করবে।
    না পেলে শেষে PORT line যোগ করবে।
    """
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        new_run = f'app.run(host="0.0.0.0", port=int(__import__("os").environ.get("PORT", {port})), debug=False)'
        pattern = r'app\.run\s*\([^)]*\)'
        new_content = re.sub(pattern, new_run, content)

        if new_content == content:
            # app.run() নেই — শেষে PORT env set করো
            new_content += f'\n# Auto-injected by NeHost\nimport os as _nehost_os\n_nehost_port = int(_nehost_os.environ.get("PORT", {port}))\n'

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    except:
        return False

# ============================================================
# DB
# ============================================================

def get_db():
    db_path = os.path.join(os.getcwd(), 'storage/nehost.db')
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    if not os.path.exists('storage'):
        os.makedirs('storage')
    db = get_db()
    db.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fname TEXT, lname TEXT, username TEXT, email TEXT, password TEXT,
        pfp TEXT DEFAULT 'default.png', role TEXT DEFAULT 'free',
        status TEXT DEFAULT 'active', server_limit INTEGER DEFAULT 1,
        notifications TEXT DEFAULT ''
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, name TEXT, folder TEXT,
        status TEXT, startup TEXT, pid INTEGER,
        port INTEGER,
        server_status TEXT DEFAULT 'active'
    )''')
    # পুরোনো DB তে port column না থাকলে add করো
    try:
        db.execute('ALTER TABLE servers ADD COLUMN port INTEGER')
        db.commit()
    except:
        pass

    db.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, subject TEXT, message TEXT,
        status TEXT DEFAULT 'open', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS admin_settings (
        id INTEGER PRIMARY KEY,
        username TEXT, password TEXT,
        popup_title TEXT, popup_msg TEXT, popup_img TEXT, show_popup INTEGER DEFAULT 0
    )''')
    if not db.execute('SELECT * FROM admin_settings WHERE id=1').fetchone():
        db.execute('INSERT INTO admin_settings (id, username, password) VALUES (1, "shirena857@gmail.com", "shihab_ff_857")')
    db.commit()
    db.close()

# ============================================================
# APP FACTORY
# ============================================================

def create_app():
    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'nehost_ultra_pro_max_99'
    app.config['BASE_STORAGE'] = os.path.join(os.getcwd(), 'storage/instances')
    app.config['UPLOAD_FOLDER'] = os.path.join(os.getcwd(), 'static/uploads')

    os.makedirs(app.config['BASE_STORAGE'], exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    init_db()
    socketio.init_app(app)

    def get_precise_uptime(start_timestamp):
        if not start_timestamp: return "Offline"
        diff = int(time.time() - start_timestamp)
        months, rem = divmod(diff, 2592000)
        days, rem = divmod(rem, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = []
        if months > 0: parts.append(f"{months}mo")
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    # ----------------------------------------------------------
    # AUTH
    # ----------------------------------------------------------
    @app.route('/')
    def home():
        return render_template('index.html')

    @app.route('/signup', methods=['GET', 'POST'])
    def signup():
        if request.method == 'POST':
            fname = request.form.get('fname')
            lname = request.form.get('lname')
            username = request.form.get('username')
            email = request.form.get('email')
            pwd = request.form.get('password')
            cpwd = request.form.get('confirm_password')
            pfp = request.files.get('pfp')
            if pwd != cpwd:
                return jsonify({'status': 'error', 'msg': 'Passwords do not match!'}), 400
            db = get_db()
            existing = db.execute('SELECT id FROM users WHERE email=? OR username=?', (email, username)).fetchone()
            if existing:
                db.close()
                return jsonify({'status': 'error', 'msg': 'Email or Username already taken!'}), 400
            pfp_name = 'default.png'
            if pfp:
                pfp_name = secure_filename(pfp.filename)
                pfp.save(os.path.join(app.config['UPLOAD_FOLDER'], pfp_name))
            db.execute('''INSERT INTO users (fname, lname, username, email, password, pfp, server_limit, role, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (fname, lname, username, email, pwd, pfp_name, 10, 'free', 'active'))
            db.commit(); db.close()
            return jsonify({'status': 'success', 'url': url_for('login')})
        return render_template('web/signup.html')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            email = request.form.get('email')
            pwd = request.form.get('password')
            db = get_db()
            user = db.execute('SELECT * FROM users WHERE (email=? OR username=?) AND password=?', (email, email, pwd)).fetchone()
            db.close()
            if user:
                if user['status'] == 'banned':
                    return jsonify({'status': 'banned', 'msg': 'Your account is suspended!'}), 403
                session['user_id'] = user['id']
                return jsonify({'status': 'success', 'url': url_for('dashboard')}), 200
            return jsonify({'status': 'error', 'msg': 'Invalid credentials!'}), 401
        return render_template('web/login.html')

    @app.route('/dashboard')
    def dashboard():
        if 'user_id' not in session: return redirect(url_for('login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        db.close()
        if not user or user['status'] != 'active':
            session.clear()
            return redirect(url_for('login'))
        return render_template('web/dashboard.html', user=user)

    @app.route('/profile/update', methods=['POST'])
    def update_profile():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        uid = session['user_id']
        fname = request.form.get('fname')
        lname = request.form.get('lname')
        pwd = request.form.get('password')
        db = get_db()
        if pwd:
            db.execute('UPDATE users SET fname=?, lname=?, password=? WHERE id=?', (fname, lname, pwd, uid))
        else:
            db.execute('UPDATE users SET fname=?, lname=? WHERE id=?', (fname, lname, uid))
        db.commit(); db.close()
        return jsonify({'status': 'success'})

    @app.route('/ticket/create', methods=['POST'])
    def create_ticket():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('INSERT INTO tickets (user_id, subject, message) VALUES (?,?,?)',
            (session['user_id'], d['subject'], d['message']))
        db.commit(); db.close()
        return jsonify({'status': 'success'})

    @app.route('/api/announcement')
    def get_announcement():
        db = get_db()
        conf = db.execute('SELECT popup_title, popup_msg, popup_img, show_popup FROM admin_settings WHERE id=1').fetchone()
        db.close()
        return jsonify(dict(conf))

    # ----------------------------------------------------------
    # ADMIN
    # ----------------------------------------------------------
    @app.route('/admin-login', methods=['GET', 'POST'])
    def admin_login():
        if request.method == 'POST':
            user, pwd = request.form.get('username'), request.form.get('password')
            db = get_db()
            admin = db.execute('SELECT * FROM admin_settings WHERE username=? AND password=?', (user, pwd)).fetchone()
            db.close()
            if admin:
                session['admin_logged'] = True
                return redirect(url_for('admin_panel'))
        return render_template('web/admin_login.html')

    @app.route('/admin/panel')
    def admin_panel():
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/admin_panel.html')

    @app.route('/admin/stats')
    def admin_stats():
        if not session.get('admin_logged'): return jsonify({})
        db = get_db()
        users = db.execute('SELECT * FROM users').fetchall()
        user_list = []
        total_cpu = psutil.cpu_percent()
        total_ram = psutil.virtual_memory().percent
        for u in users:
            srvs = db.execute('SELECT * FROM servers WHERE user_id=?', (u['id'],)).fetchall()
            active_srvs = 0
            for s in srvs:
                is_on = False
                if s['pid'] and psutil.pid_exists(s['pid']):
                    try:
                        proc = psutil.Process(s['pid'])
                        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                            is_on = True
                    except: pass
                elif s['folder'] in running_procs and running_procs[s['folder']].poll() is None:
                    is_on = True
                if is_on: active_srvs += 1
            user_list.append({
                'id': u['id'], 'fname': u['fname'], 'email': u['email'],
                'srv_count': len(srvs), 'active_srvs': active_srvs,
                'status': u['status'], 'role': u['role'], 'server_limit': u['server_limit']
            })
        db.close()
        return jsonify({'users': user_list, 'sys_cpu': f"{total_cpu}%", 'sys_ram': f"{total_ram}%"})

    @app.route('/admin/user/update', methods=['POST'])
    def update_user():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET role=?, status=?, server_limit=? WHERE id=?',
            (d['role'], d['status'], d['limit'], d['user_id']))
        db.commit(); db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/set-popup', methods=['POST'])
    def set_popup():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        title, msg, show = request.form.get('title'), request.form.get('msg'), request.form.get('show')
        img = request.files.get('image')
        db = get_db()
        old_data = db.execute('SELECT popup_img FROM admin_settings WHERE id=1').fetchone()
        img_name = old_data['popup_img'] if old_data else None
        if img:
            img_name = secure_filename(img.filename)
            img.save(os.path.join(app.config['UPLOAD_FOLDER'], img_name))
        db.execute('UPDATE admin_settings SET popup_title=?, popup_msg=?, popup_img=?, show_popup=? WHERE id=1',
            (title, msg, img_name, 1 if show == 'true' else 0))
        db.commit(); db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/send-warning', methods=['POST'])
    def send_warning():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('UPDATE users SET notifications=? WHERE id=?', (d['message'], d['user_id']))
        db.commit(); db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/login-as/<int:uid>')
    def login_as(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        session['user_id'] = uid
        return redirect(url_for('dashboard'))

    @app.route('/admin/manage-user/<int:uid>')
    def admin_manage_user_servers(uid):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (uid,)).fetchall()
        db.close()
        servers = []
        for r in rows:
            f = r['folder']
            online = (f in running_procs and running_procs[f].poll() is None) or (r['pid'] and psutil.pid_exists(r['pid']))
            servers.append({'id': r['id'], 'name': r['name'], 'folder': f, 'online': online, 'status': r['server_status']})
        return render_template('web/admin_manage_user.html', user=user, servers=servers)

    @app.route('/admin/suspend-server/<int:sid>', methods=['POST'])
    def admin_suspend_server(sid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        status = request.json.get('status')
        db = get_db()
        db.execute('UPDATE servers SET server_status=? WHERE id=?', (status, sid))
        db.commit(); db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-server/<int:sid>', methods=['POST'])
    def admin_delete_server(sid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        db = get_db()
        srv = db.execute('SELECT folder FROM servers WHERE id=?', (sid,)).fetchone()
        if srv:
            folder = srv['folder']
            if folder in running_procs:
                try: os.killpg(os.getpgid(running_procs[folder].pid), signal.SIGKILL)
                except: pass
                del running_procs[folder]
            db.execute('DELETE FROM servers WHERE id=?', (sid,))
            db.commit()
            path = os.path.join(app.config['BASE_STORAGE'], folder)
            if os.path.exists(path): shutil.rmtree(path)
            db.close()
            return jsonify({'status': 'deleted'})
        db.close()
        return jsonify({'status': 'error', 'msg': 'Server not found'})

    @app.route('/admin/create-user', methods=['POST'])
    def admin_create_user():
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        d = request.json
        db = get_db()
        db.execute('INSERT INTO users (fname, email, password, server_limit) VALUES (?,?,?,?)',
            (d['name'], d['email'], d['pass'], d.get('limit', 1)))
        db.commit(); db.close()
        return jsonify({'status': 'success'})

    @app.route('/admin/delete-user/<int:uid>', methods=['POST'])
    def delete_user(uid):
        if not session.get('admin_logged'): return jsonify({'status': 'error'})
        db = get_db()
        srvs = db.execute('SELECT folder FROM servers WHERE user_id=?', (uid,)).fetchall()
        for s in srvs:
            path = os.path.join(app.config['BASE_STORAGE'], s['folder'])
            if os.path.exists(path): shutil.rmtree(path)
        db.execute('DELETE FROM servers WHERE user_id=?', (uid,))
        db.execute('DELETE FROM users WHERE id=?', (uid,))
        db.commit(); db.close()
        return jsonify({'status': 'deleted'})

    @app.route('/admin/files/<folder>')
    def admin_browse_files(folder):
        if not session.get('admin_logged'): return redirect(url_for('admin_login'))
        return render_template('web/dashboard.html', user={'fname': 'Admin'}, is_admin_view=True, admin_folder=folder)

    # ----------------------------------------------------------
    # FILE MANAGER
    # ----------------------------------------------------------
    @app.route('/files/list/<folder>')
    def flist(folder):
        sub_path = request.args.get('path', '')
        full_path = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path))
        if not full_path.startswith(app.config['BASE_STORAGE']): return jsonify([])
        if not os.path.exists(full_path): return jsonify([])
        items = []
        for f in sorted(os.listdir(full_path)):
            if f == 'console.log': continue
            p = os.path.join(full_path, f)
            items.append({'name': f, 'is_dir': os.path.isdir(p), 'is_zip': f.lower().endswith('.zip'), 'rel_path': os.path.join(sub_path, f)})
        return jsonify(items)

    @app.route('/files/content/<folder>/<name>')
    def fcontent(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name)
        try:
            with open(p, 'r', encoding='utf-8', errors='ignore') as f: return jsonify({'content': f.read()})
        except: return jsonify({'content': 'Error reading file'})

    @app.route('/files/save/<folder>/<name>', methods=['POST'])
    def fsave(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name)
        try:
            with open(p, 'w', encoding='utf-8') as f: f.write(request.json.get('content'))
            return jsonify({'status': 'saved'})
        except: return jsonify({'status': 'error'})

    @app.route('/files/delete-bulk/<folder>', methods=['POST'])
    def delete_bulk(folder):
        d = request.json
        sub_path, names = d.get('path', ''), d.get('names', [])
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        for name in names:
            p = os.path.join(base, name)
            if name == 'console.log': continue
            try:
                if os.path.isdir(p): shutil.rmtree(p)
                elif os.path.exists(p): os.remove(p)
            except: pass
        return jsonify({"status": "ok"})

    @app.route('/files/create-file/<folder>', methods=['POST'])
    def create_file(folder):
        d = request.json
        p = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''), secure_filename(d.get('name')))
        with open(p, 'w') as f: f.write("")
        return jsonify({'status': 'success'})

    @app.route('/files/create-folder/<folder>', methods=['POST'])
    def create_folder(folder):
        d = request.json
        p = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''), secure_filename(d.get('name')))
        os.makedirs(p, exist_ok=True)
        return jsonify({'status': 'success'})

    @app.route('/files/upload/<folder>', methods=['POST'])
    def upload_file(folder):
        sub_path = request.form.get('path', '')
        file = request.files['file']
        dest = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        os.makedirs(dest, exist_ok=True)
        file.save(os.path.join(dest, secure_filename(file.filename)))
        return jsonify({'status': 'success'})

    @app.route('/files/rename/<folder>', methods=['POST'])
    def rename_file(folder):
        d = request.json
        base = os.path.join(app.config['BASE_STORAGE'], folder, d.get('path', ''))
        os.rename(os.path.join(base, d['old']), os.path.join(base, d['new']))
        return jsonify({'status': 'success'})

    @app.route('/files/download/<folder>/<name>')
    def download_file(folder, name):
        sub_path = request.args.get('path', '')
        p = os.path.normpath(os.path.join(app.config['BASE_STORAGE'], folder, sub_path, name))
        if not p.startswith(app.config['BASE_STORAGE']): return "Access Denied", 403
        return send_file(p, as_attachment=True)

    @app.route('/files/zip-bulk/<folder>', methods=['POST'])
    def zip_bulk(folder):
        d = request.json
        names, sub_path = d.get('names', []), d.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        if not names: names = [f for f in os.listdir(base) if f != 'console.log']
        zip_name = f"archive_{int(time.time())}.zip"
        zip_path = os.path.join(base, zip_name)
        with zipfile.ZipFile(zip_path, 'w') as z:
            for n in names:
                p = os.path.join(base, n)
                if n == zip_name: continue
                if os.path.isdir(p):
                    for root, dirs, files in os.walk(p):
                        for file in files:
                            full_p = os.path.join(root, file)
                            z.write(full_p, os.path.relpath(full_p, base))
                elif os.path.exists(p): z.write(p, n)
        return jsonify({'status': 'success', 'zip': zip_name})

    @app.route('/files/unzip/<folder>', methods=['POST'])
    def unzip_file(folder):
        d = request.json
        zip_name = d.get('name')
        sub_path = d.get('path', '')
        base = os.path.join(app.config['BASE_STORAGE'], folder, sub_path)
        zip_path = os.path.join(base, zip_name)
        if os.path.exists(zip_path) and zipfile.is_zipfile(zip_path):
            try:
                with zipfile.ZipFile(zip_path, 'r') as z:
                    z.extractall(base)
                return jsonify({'status': 'success'})
            except Exception as e:
                return jsonify({'status': 'error', 'msg': str(e)})
        return jsonify({'status': 'error', 'msg': 'Invalid zip file'})

    # ----------------------------------------------------------
    # SERVER CONTROL
    # ----------------------------------------------------------
    @app.route('/server/action/<folder>/<act>', methods=['POST'])
    def server_action(folder, act):
        db = get_db()
        srv_data = db.execute('SELECT server_status FROM servers WHERE folder=?', (folder,)).fetchone()
        if srv_data and srv_data['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'This server is suspended by Admin.'})

        path = os.path.join(app.config['BASE_STORAGE'], folder)
        log_file_path = os.path.join(path, 'console.log')
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if act == 'install':
            req_path = os.path.join(path, 'requirements.txt')
            if os.path.exists(req_path):
                f_log = open(log_file_path, 'a')
                f_log.write(f"\n[{now}] Package Installation Started...\n")
                f_log.flush()
                subprocess.Popen(['pip', 'install', '-r', 'requirements.txt'], cwd=path, stdout=f_log, stderr=f_log)
                db.close()
                return jsonify({'status': 'installing'})
            db.close()
            return jsonify({'status': 'error', 'msg': 'requirements.txt missing'})

        if act in ['start', 'restart']:
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            old_pid = row['pid'] if row else None
            if folder in running_procs or (old_pid and psutil.pid_exists(old_pid)):
                try:
                    t_pid = running_procs[folder].pid if folder in running_procs else old_pid
                    os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass

            srv = db.execute('SELECT startup FROM servers WHERE folder=?', (folder,)).fetchone()
            startup_file = srv['startup'] if srv and srv['startup'] else 'main.py'
            startup_full_path = os.path.join(path, startup_file)

            # ✅ Free port নাও
            port = get_free_port()

            # ✅ Startup file এ PORT auto inject করো
            if port and os.path.exists(startup_full_path):
                inject_port(startup_full_path, port)

            # ✅ Public URL বানাও
            url = get_public_url(port, request) if port else None

            f_log = open(log_file_path, 'a')
            f_log.write(f"\n[{now}] Instance {act.upper()}ED\n")
            if url:
                f_log.write(f"[{now}] URL: {url}\n")
            f_log.flush()

            env = os.environ.copy()
            if port:
                env['PORT'] = str(port)

            proc = subprocess.Popen(
                ['python3', startup_file],
                cwd=path, stdout=f_log, stderr=f_log,
                preexec_fn=os.setsid, env=env
            )
            running_procs[folder] = proc
            start_times[folder] = time.time()

            db.execute('UPDATE servers SET pid=?, port=? WHERE folder=?', (proc.pid, port, folder))
            db.commit(); db.close()

            return jsonify({'status': 'started', 'port': port, 'url': url})

        elif act == 'stop':
            row = db.execute('SELECT pid FROM servers WHERE folder=?', (folder,)).fetchone()
            t_pid = running_procs[folder].pid if folder in running_procs else (row['pid'] if row else None)
            if t_pid:
                try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
                except: pass
            if folder in running_procs: del running_procs[folder]
            db.execute('UPDATE servers SET pid=NULL, port=NULL WHERE folder=?', (folder,))
            db.commit(); db.close()
            with open(log_file_path, 'a') as f:
                f.write(f"\n[{now}] Instance STOPPED\n")
            return jsonify({'status': 'stopped'})

        db.close()
        return jsonify({'status': 'ok'})

    @app.route('/server/log/<folder>')
    def server_log(folder):
        path = os.path.join(app.config['BASE_STORAGE'], folder, 'console.log')
        if os.path.exists(path):
            with open(path, 'r') as f: return jsonify({'log': f.read()[-5000:]})
        return jsonify({'log': 'Waiting for logs...'})

    @app.route('/server/set-startup/<folder>', methods=['POST'])
    def set_startup(folder):
        cmd = request.json.get('file')
        db = get_db()
        db.execute('UPDATE servers SET startup=? WHERE folder=?', (cmd, folder))
        db.commit(); db.close()
        return jsonify({'status': 'success'})

    @app.route('/server/delete/<folder>', methods=['POST'])
    def delete_server(folder):
        if 'user_id' not in session:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        db = get_db()
        srv = db.execute('SELECT user_id, server_status, pid FROM servers WHERE folder=?', (folder,)).fetchone()
        if not srv:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Server not found'})
        if srv['user_id'] != session['user_id']:
            db.close()
            return jsonify({'status': 'error', 'msg': 'Access denied'})
        if srv['server_status'] == 'suspended':
            db.close()
            return jsonify({'status': 'error', 'msg': 'Suspended servers cannot be deleted!'})
        t_pid = running_procs[folder].pid if folder in running_procs else (srv['pid'] if srv else None)
        if t_pid:
            try: os.killpg(os.getpgid(t_pid), signal.SIGKILL)
            except: pass
        if folder in running_procs: del running_procs[folder]
        db.execute('DELETE FROM servers WHERE folder=?', (folder,))
        db.commit(); db.close()
        path = os.path.join(app.config['BASE_STORAGE'], folder)
        if os.path.exists(path): shutil.rmtree(path)
        return jsonify({'status': 'deleted'})

    @app.route('/servers')
    def list_servers():
        if 'user_id' not in session: return jsonify({'servers': []})
        db = get_db()
        rows = db.execute('SELECT * FROM servers WHERE user_id=?', (session['user_id'],)).fetchall()
        db.close()
        srvs = []
        for r in rows:
            f, saved_pid = r['folder'], r['pid']
            online = False
            if saved_pid and psutil.pid_exists(saved_pid):
                try:
                    p = psutil.Process(saved_pid)
                    if p.is_running() and p.status() != psutil.STATUS_ZOMBIE: online = True
                except: pass
            elif f in running_procs and running_procs[f].poll() is None:
                online = True
            uptime = get_precise_uptime(start_times.get(f)) if online and f in start_times else ("Online" if online else "Offline")
            cpu, ram = "0%", "0MB"
            if online:
                try:
                    p_pid = running_procs[f].pid if f in running_procs else saved_pid
                    process = psutil.Process(p_pid)
                    cpu = f"{process.cpu_percent(interval=None)}%"
                    ram = f"{process.memory_info().rss / (1024 * 1024):.1f}MB"
                except: pass

            port = r['port'] if 'port' in r.keys() else None
            url = get_public_url(port, request) if (online and port) else None

            srvs.append({
                'name': r['name'], 'folder': f, 'online': online,
                'startup': r['startup'], 'uptime': uptime,
                'cpu': cpu, 'ram': ram, 'status': r['server_status'],
                'port': port, 'url': url
            })
        return jsonify({'servers': srvs})

    @app.route('/add', methods=['POST'])
    def add_srv():
        if 'user_id' not in session: return jsonify({'status': 'error'})
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        count = db.execute('SELECT COUNT(*) as count FROM servers WHERE user_id=?', (session['user_id'],)).fetchone()['count']
        if user['role'] != 'admin' and count >= user['server_limit']:
            db.close()
            return jsonify({'status': 'error', 'msg': f"Limit Reached! Max: {user['server_limit']}"})
        name = request.json.get('name')
        folder = secure_filename(name).lower() + "_" + str(int(time.time()))
        db.execute('INSERT INTO servers (user_id, name, folder, status, startup) VALUES (?,?,?,?,?)',
            (session['user_id'], name, folder, 'Offline', 'main.py'))
        db.commit(); db.close()
        os.makedirs(os.path.join(app.config['BASE_STORAGE'], folder), exist_ok=True)
        return jsonify({'status': 'success'})

    return app

# ============================================================
# MAIN — Terminal এ run করলে URL দেখাবে
# ============================================================
app = create_app()

if __name__ == "__main__":
    PANEL_PORT = 5100

    public_url = None

    # 1. CodeSandbox detect
    csb_id = os.environ.get("CSB_SANDBOX_ID", "")
    if csb_id:
        public_url = f"https://{csb_id}-{PANEL_PORT}.csb.app"

    # 2. ngrok try করো (pyngrok installed থাকলে)
    if not public_url:
        try:
            from pyngrok import ngrok
            tunnel = ngrok.connect(PANEL_PORT, "http")
            public_url = tunnel.public_url
            print(f"✅ ngrok tunnel created!")
        except ImportError:
            pass
        except Exception as e:
            print(f"ngrok error: {e}")

    # 3. fallback localhost
    if not public_url:
        public_url = f"http://localhost:{PANEL_PORT}"

    print("\n" + "="*55)
    print("   NeHost Panel চালু হয়েছে!")
    print(f"   Public URL : {public_url}")
    print(f"   Local  URL : http://localhost:{PANEL_PORT}")
    print("="*55)
    print("   Hosted app গুলো আলাদা port এ চলবে (5100+)")
    print("   Dashboard এ প্রতিটা server এর URL দেখাবে।")
    print("="*55 + "\n")

    socketio.run(app, host='0.0.0.0', port=PANEL_PORT, debug=False)

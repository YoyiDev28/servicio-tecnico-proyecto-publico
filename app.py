import os
import io
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate, upgrade
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import case, func, desc, and_, MetaData, Enum
from collections import defaultdict
from functools import wraps
import enum
import qrcode
import base64
from io import BytesIO

# --- 1. Configuración de la Aplicación y la Base de Datos ---
basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    """Configuración principal de la aplicación."""
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///' + os.path.join(basedir, 'instance', 'site.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'una_clave_muy_secreta_y_aleatoria'
    UPLOAD_FOLDER = os.path.join(basedir, 'static', 'uploads')
    
app = Flask(__name__)
app.config.from_object(Config)

# Asegura que el directorio de subidas exista
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Configura la convención de nombres para las restricciones
convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}

metadata = MetaData(naming_convention=convention)
db = SQLAlchemy(app, metadata=metadata)
migrate = Migrate(app, db)


# --- 2. Modelos de la Base de Datos (SQLAlchemy) ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='tecnico')
    branch = db.Column(db.String(50), nullable=True, default='Sucursal Principal')
    
    registered_devices = db.relationship(
        'Device', 
        backref='registrant', 
        lazy=True,
        foreign_keys='Device.user_id',
        overlaps="assigned_devices" 
    )
    assigned_devices = db.relationship(
        'Device', 
        backref='technician', 
        lazy=True,
        foreign_keys='Device.assigned_technician_id',
        overlaps="registered_devices" 
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tracking_code = db.Column(db.String(20), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False) 
    assigned_technician_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) 
    branch = db.Column(db.String(50), nullable=False, default='Sucursal Principal')
    brand = db.Column(db.String(100), nullable=False)
    model = db.Column(db.String(100), nullable=False)
    serial_number = db.Column(db.String(100), unique=True, nullable=True)
    problem_description = db.Column(db.Text, nullable=False)
    initial_condition_photo_path = db.Column(db.String(512), nullable=True)
    current_status = db.Column(db.Enum('Ingresado', 'Observacion', 'Reparacion', 'Terminado', 'Retirado', name='status_enum'), default='Ingresado')
    
    customer_full_name = db.Column(db.String(100), nullable=False)
    customer_id_number = db.Column(db.String(20), nullable=True)
    
    customer_phone = db.Column(db.String(20), nullable=False)
    customer_email = db.Column(db.String(100), nullable=True)
    reception_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    repairs = db.relationship('Repair', backref='device', lazy=True)
    final_price = db.Column(db.Float, nullable=True)
    delivery_date = db.Column(db.DateTime, nullable=True)

class Repair(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    description = db.Column(db.Text, nullable=False)
    start_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    end_date = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(50), nullable=False, default='Pendiente')
    notes = db.Column(db.Text, nullable=True)
    cost = db.Column(db.Float, nullable=False, default=0.0)
    price_to_customer = db.Column(db.Float, nullable=False, default=0.0)
    repair_photo_path = db.Column(db.String(255), nullable=True) 
    components_used = db.relationship('RepairComponent', backref='repair', lazy=True)

class Component(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    stock_quantity = db.Column(db.Integer, nullable=False, default=0)
    price = db.Column(db.Float, nullable=False)
    repairs_used_in = db.relationship('RepairComponent', backref='component', lazy=True)

class RepairComponent(db.Model):
    repair_id = db.Column(db.Integer, db.ForeignKey('repair.id'), primary_key=True)
    component_id = db.Column(db.Integer, db.ForeignKey('component.id'), primary_key=True)
    quantity_used = db.Column(db.Integer, nullable=False, default=1)

# --- 3. Funciones de Utilidad y Decoradores ---
def requires_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            flash('Por favor, inicia sesión para acceder a esta página.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def requires_roles(*roles):
    def wrapper(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if 'username' not in session or session.get('role') not in roles:
                flash('No tienes permiso para acceder a esta página. Por favor, inicia sesión con una cuenta válida.', 'error')
                return redirect(url_for('login'))
            return f(*args, **kwargs)
        return wrapped
    return wrapper

# --- NUEVA FUNCIÓN PARA SERVIR ARCHIVOS SUBIDOS ---
@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- 4. Rutas de la Aplicación ---
@app.route('/')
def home():
    return render_template('home.html')

@app.context_processor
def inject_now():
    return {'now': datetime.utcnow()}

@app.route('/track', methods=['GET', 'POST'])
def track_device():
    device = None
    if request.method == 'POST':
        terms_accepted = request.form.get('terms_acceptance')
        if not terms_accepted:
            flash('Debes aceptar los Términos y Condiciones para ver el estado del dispositivo.', 'warning')
            return redirect(url_for('track_device'))

        tracking_code = request.form.get('tracking_code')
        customer_id_number = request.form.get('customer_id_number')
        if tracking_code and customer_id_number:
            device = Device.query.filter_by(tracking_code=tracking_code, customer_id_number=customer_id_number).first()
            if not device:
                flash('Código de seguimiento o DNI/CUIT no encontrados o no coinciden. Por favor, verifica e inténtalo de nuevo.', 'error')
        else:
            flash('Por favor, ingresa tanto el código de seguimiento como el DNI/CUIT.', 'warning')
    return render_template('track_device.html', device=device)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            session['logged_in'] = True
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            session['branch'] = user.branch
            flash('Inicio de sesión exitoso.', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Nombre de usuario o contraseña incorrectos.', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Has cerrado sesión exitosamente.', 'info')
    return redirect(url_for('home'))

@app.route('/track/<string:tracking_code>')
def track_device_status(tracking_code):
    device = Device.query.filter_by(tracking_code=tracking_code).first_or_404()
    warning_message = None
    if device.current_status == 'Terminado':
        last_repair = Repair.query.filter_by(device_id=device.id, status='Terminado').order_by(Repair.end_date.desc()).first()
        if last_repair and last_repair.end_date:
            days_since_completion = (datetime.utcnow() - last_repair.end_date).days
            if days_since_completion > 5:
                warning_message = f"¡Atención! Han pasado {days_since_completion} días desde la finalización. La garantía ha expirado."
            elif days_since_completion > 3: 
                remaining_days = 5 - days_since_completion
                warning_message = f"¡Importante! Tienes {remaining_days} días restantes para retirar tu dispositivo y conservar la garantía."
    return render_template('public_status.html', device=device, warning_message=warning_message, warranty_days_text='5')

@app.route('/ticket/<string:tracking_code>')
def generate_ticket(tracking_code):
    device = Device.query.filter_by(tracking_code=tracking_code).first_or_404()
    warranty_end_date = device.reception_date + timedelta(days=5)
    terminos_url = url_for('track_device_status', tracking_code=device.tracking_code, _external=True)
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=5,
        border=4,
    )
    qr.add_data(terminos_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    
    return render_template(
        'ticket.html', 
        device=device,
        warranty_end_date=warranty_end_date.strftime('%d/%m/%Y %H:%M'),
        qr_terminos=img_str
    )

@app.route('/admin')
@requires_roles('admin', 'administrativo', 'vendedor', 'tecnico')
def admin_dashboard():
    return render_template('admin_dashboard.html')

@app.route('/admin/devices', methods=['GET'])
@requires_roles('admin', 'administrativo', 'tecnico', 'vendedor')
def list_devices():
    search_query = request.args.get('query', '')
    query = Device.query
    
    last_repair_date_subquery = db.session.query(
        func.max(Repair.end_date).label('last_end_date')
    ).filter(
        Repair.device_id == Device.id,
        Repair.status == 'Terminado'
    ).scalar_subquery()

    five_days_ago = datetime.utcnow() - timedelta(days=5)
    priority_order = case(
        (and_(Device.current_status == 'Terminado', last_repair_date_subquery > five_days_ago), 0),
        (and_(Device.current_status == 'Terminado', last_repair_date_subquery <= five_days_ago), 1),
        else_=2
    )
    
    if search_query:
        try:
            device_id = int(search_query)
            query = query.filter(Device.id == device_id)
        except ValueError:
            search_term = f"%{search_query}%"
            query = query.filter(
                (Device.tracking_code.ilike(search_term)) |
                (Device.customer_full_name.ilike(search_term)) |
                (Device.customer_id_number.ilike(search_term)) |
                (Device.brand.ilike(search_term)) |
                (Device.model.ilike(search_term))
            )
    
    devices = query.order_by(
        priority_order,
        last_repair_date_subquery.asc()
    ).all()

    return render_template('list_devices.html', devices=devices)

# --- GESTION DE USUARIOS ---
@app.route('/admin/manage_users', methods=['GET', 'POST'])
@requires_roles('admin')
def manage_users():
    users = User.query.all()
    roles = ['admin', 'administrativo', 'vendedor', 'tecnico']
    branches = ['Sucursal Principal', 'Sucursal Norte', 'Sucursal Sur']
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            username = request.form.get('username')
            password = request.form.get('password')
            role = request.form.get('role')
            branch = request.form.get('branch') or 'Sucursal Principal'
            
            if not username or not password or not role:
                flash('Por favor, completa todos los campos para crear un usuario.', 'danger')
                return redirect(url_for('manage_users'))

            existing_user = User.query.filter_by(username=username).first()
            if existing_user:
                flash(f'El usuario "{username}" ya existe.', 'danger')
            else:
                new_user = User(username=username, role=role, branch=branch)
                new_user.set_password(password)
                try:
                    db.session.add(new_user)
                    db.session.commit()
                    flash(f'Usuario "{username}" creado con éxito.', 'success')
                except Exception as e:
                    db.session.rollback()
                    flash(f'Error al crear el usuario: {str(e)}', 'danger')
        
        elif action == 'delete':
            user_id = request.form.get('user_id')
            user_to_delete = User.query.get(user_id)
            if user_to_delete and user_to_delete.username != 'Admin' and user_to_delete.id != session.get('user_id'):
                try:
                    devices_to_reassign = Device.query.filter(
                        (Device.user_id == user_id) | (Device.assigned_technician_id == user_id)
                    ).all()
                    
                    for device in devices_to_reassign:
                        if device.user_id == user_id:
                            device.user_id = session.get('user_id')
                        if device.assigned_technician_id == user_id:
                            device.assigned_technician_id = None 

                    db.session.delete(user_to_delete)
                    db.session.commit()
                    flash(f'Usuario "{user_to_delete.username}" eliminado con éxito.', 'success')
                except Exception as e:
                    db.session.rollback()
                    flash(f'Error al eliminar el usuario: {str(e)}', 'danger')
            else:
                flash('No se puede eliminar un usuario administrador o a ti mismo.', 'danger')

    return render_template('manage_users.html', users=users, roles=roles, branches=branches)

@app.route('/admin/change_password/<int:user_id>', methods=['POST'])
@requires_roles('admin')
def change_password(user_id):
    user_to_change = User.query.get_or_404(user_id)
    new_password = request.form.get('new_password')
    if not new_password:
        flash('La nueva contraseña no puede estar vacía.', 'danger')
    else:
        user_to_change.set_password(new_password)
        db.session.commit()
        flash(f'La contraseña del usuario {user_to_change.username} ha sido cambiada con éxito.', 'success')
    return redirect(url_for('manage_users'))

# --- RUTA REFORMULADA DE REPORTE DE INGRESOS ---
@app.route('/admin/revenue_report')
@requires_roles('admin', 'administrativo')
def revenue_report():
    delivered_devices = Device.query.filter(Device.current_status == 'Retirado').all()
    
    monthly_revenue = defaultdict(float)
    weekly_revenue = defaultdict(float)
    daily_revenue = defaultdict(float)

    monthly_profit = defaultdict(float)
    weekly_profit = defaultdict(float)
    daily_profit = defaultdict(float)
    
    for device in delivered_devices:
        if device.delivery_date and device.final_price is not None:
            month_year = device.delivery_date.strftime('%Y-%B')
            monthly_revenue[month_year] += device.final_price
            
            week_year = f"{device.delivery_date.year}-{device.delivery_date.isocalendar()[1]}"
            weekly_revenue[week_year] += device.final_price
            
            day_date = device.delivery_date.strftime('%Y-%m-%d')
            daily_revenue[day_date] += device.final_price
            
            total_repair_cost = sum(repair.cost for repair in device.repairs)
            net_profit = device.final_price - total_repair_cost
            
            monthly_profit[month_year] += net_profit
            weekly_profit[week_year] += net_profit
            daily_profit[day_date] += net_profit
            
    return render_template(
        'revenue_report.html',
        monthly_revenue=monthly_revenue,
        weekly_revenue=weekly_revenue,
        daily_revenue=daily_revenue,
        monthly_profit=monthly_profit,
        weekly_profit=weekly_profit,
        daily_profit=daily_profit
    )
    
# --- RUTA PARA EDITAR COSTO DE REPARACIÓN ---
@app.route('/admin/repair/<int:repair_id>/edit_cost', methods=['POST'])
@requires_roles('admin')
def edit_repair_cost(repair_id):
    repair = Repair.query.get_or_404(repair_id)
    new_cost_str = request.form.get('new_cost')
    
    if new_cost_str is not None:
        try:
            new_cost = float(new_cost_str)
            
            repair.cost = new_cost
            db.session.commit()
            
            flash('Costo de reparación actualizado con éxito.', 'success')
        except (ValueError, TypeError):
            db.session.rollback()
            flash('El costo debe ser un número válido.', 'danger')
    
    return redirect(url_for('view_device_details', device_id=repair.device_id))

@app.route('/admin/repair/<int:repair_id>/edit_price', methods=['POST'])
@requires_roles('admin')
def edit_repair_price(repair_id):
    repair = Repair.query.get_or_404(repair_id)
    new_price_str = request.form.get('new_price_to_customer')

    if new_price_str is not None:
        try:
            new_price = float(new_price_str)
            repair.price_to_customer = new_price
            db.session.commit()
            flash('Precio al cliente actualizado con éxito.', 'success')
        except (ValueError, TypeError):
            db.session.rollback()
            flash('El precio debe ser un número válido.', 'danger')
    
    return redirect(url_for('view_device_details', device_id=repair.device_id))


@app.route('/admin/add_device', methods=['GET', 'POST'])
@requires_roles('admin', 'vendedor')
def add_device():
    if request.method == 'POST':
        customer_full_name = request.form.get('customer_full_name')
        customer_id_number = request.form.get('customer_id_number')
        customer_phone = request.form.get('customer_phone')
        customer_email = request.form.get('customer_email')
        brand = request.form.get('brand')
        model = request.form.get('model')
        serial_number = request.form.get('serial_number')
        problem_description = request.form.get('problem_description')
        tracking_code = 'OT-' + datetime.utcnow().strftime('%Y%m%d%H%M%S')
        
        photos_urls = []
        if 'initial_photos[]' in request.files:
            files = request.files.getlist('initial_photos[]')
            for file in files:
                if file and file.filename != '':
                    filename = secure_filename(f"{tracking_code}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{file.filename}")
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(filepath)
                    photos_urls.append(filename) 

        initial_photo_path_string = ",".join(photos_urls)
        
        new_device = Device(
            customer_full_name=customer_full_name,
            customer_id_number=customer_id_number,
            customer_phone=customer_phone,
            customer_email=customer_email,
            brand=brand,
            model=model,
            serial_number=serial_number,
            problem_description=problem_description,
            tracking_code=tracking_code,
            initial_condition_photo_path=initial_photo_path_string,
            user_id=session.get('user_id'),
            branch='Sucursal Principal'
        )
        
        try:
            db.session.add(new_device)
            db.session.commit()
            flash(f'Dispositivo registrado con éxito. Código: {tracking_code}', 'success')
            return redirect(url_for('generate_ticket', tracking_code=new_device.tracking_code))
        except Exception as e:
            db.session.rollback()
            flash(f'Error al registrar el dispositivo: {str(e)}', 'error')
            
    return render_template('add_device.html')

@app.route('/admin/device/<int:device_id>/delete', methods=['POST'])
@requires_roles('admin')
def delete_device(device_id):
    """
    Ruta para eliminar un dispositivo. Accesible solo para administradores.
    """
    device = Device.query.get_or_404(device_id)
    
    # Inicia la transacción para eliminar el dispositivo y sus reparaciones asociadas
    try:
        # Elimina las reparaciones asociadas para evitar errores de restricción de clave externa
        for repair in device.repairs:
            db.session.delete(repair)
        
        # Elimina el dispositivo
        db.session.delete(device)
        db.session.commit()
        
        flash(f'El dispositivo con código {device.tracking_code} ha sido eliminado exitosamente.', 'success')
        return redirect(url_for('admin_dashboard'))
    except Exception as e:
        db.session.rollback()
        flash(f'Ocurrió un error al intentar eliminar el dispositivo: {str(e)}', 'danger')
        return redirect(url_for('view_device_details', device_id=device.id))


@app.route('/admin/device/<int:device_id>', methods=['GET', 'POST'])
@requires_roles('admin', 'administrativo', 'vendedor', 'tecnico')
def view_device_details(device_id):
    device = Device.query.get_or_404(device_id)
    technicians = User.query.filter_by(role='tecnico').all()

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'mark_delivered':
            if session.get('role') not in ['admin', 'vendedor']:
                flash('No tienes permiso para marcar un dispositivo como entregado.', 'error')
                return redirect(url_for('view_device_details', device_id=device.id))
            final_price_str = request.form.get('final_price')
            if final_price_str:
                try:
                    final_price = float(final_price_str)
                    device.final_price = final_price
                    device.delivery_date = datetime.utcnow()
                    device.current_status = 'Retirado'
                    db.session.commit()
                    flash(f'Dispositivo entregado exitosamente. Se ha registrado un cobro de ${final_price:.2f}.', 'success')
                except (ValueError, TypeError):
                    flash('El precio final debe ser un número válido.', 'error')
            else:
                flash('Por favor, ingresa el precio final para marcar como entregado.', 'warning')

        elif action == 'revert_status':
            if session.get('role') != 'admin':
                flash('No tienes permiso para revertir el estado del dispositivo.', 'error')
                return redirect(url_for('view_device_details', device_id=device.id))
            device.current_status = 'Terminado'
            device.final_price = None
            device.delivery_date = None
            db.session.commit()
            flash('El estado del dispositivo ha sido revertido a "Terminado".', 'info')

        elif action == 'assign_technician':
            if session.get('role') not in ['admin', 'administrativo']:
                flash('No tienes permiso para asignar un técnico.', 'error')
                return redirect(url_for('view_device_details', device_id=device.id))

            assigned_technician_id = request.form.get('technician_id')

            if assigned_technician_id:
                device.assigned_technician_id = int(assigned_technician_id)
                device.current_status = 'Observacion'
                db.session.commit()
                flash('Técnico asignado con éxito. El estado ha cambiado a Observación.', 'success')
            else:
                flash('Por favor, selecciona un técnico.', 'warning')

        elif action == 'update_status':
            if session.get('role') not in ['admin', 'administrativo', 'tecnico']:
                flash('No tienes permiso para cambiar el estado.', 'error')
                return redirect(url_for('view_device_details', device_id=device.id))

            new_status = request.form.get('current_status')
            if new_status:
                device.current_status = new_status
                db.session.commit()
                flash(f'Estado del dispositivo actualizado a "{new_status}".', 'success')
            else:
                flash('No se seleccionó un estado válido.', 'warning')

        return redirect(url_for('view_device_details', device_id=device.id))

    return render_template('device_details.html', device=device, technicians=technicians)
    
    # Lógica para manejar la solicitud GET
    # Aquí es donde se construye la lista 'all_photos'
    all_photos = []
    if device.initial_condition_photo_path:
        all_photos.extend(device.initial_condition_photo_path.split(','))
        
    for repair in device.repairs:
        if repair.repair_photo_path:
            all_photos.extend(repair.repair_photo_path.split(','))

    return render_template('device_details.html', device=device, technicians=technicians, all_photos=all_photos)

    
    # Lógica para manejar la solicitud GET
    initial_photos = device.initial_condition_photo_path.split(',') if device.initial_condition_photo_path else []
    
    all_photos = list(initial_photos)
    for repair in device.repairs:
        if repair.repair_photo_path:
            repair_photos = repair.repair_photo_path.split(',')
            for photo_filename in repair_photos:
                all_photos.append(photo_filename.strip())

    return render_template('device_details.html', device=device, technicians=technicians, all_photos=all_photos)

@app.route('/admin/device/<int:device_id>/add_repair', methods=['GET', 'POST'])
@requires_roles('admin', 'administrativo', 'tecnico')
def add_repair(device_id):
    device = Device.query.get_or_404(device_id)
    if request.method == 'POST':
        description = request.form.get('description')
        status = request.form.get('status')
        notes = request.form.get('notes')
        cost = request.form.get('cost')
        price_to_customer = request.form.get('price_to_customer')

        repair_photo_path = None
        if 'repair_photo' in request.files:
            file = request.files['repair_photo']
            if file and file.filename != '':
                filename = secure_filename(f"{device.tracking_code}_repair_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{file.filename}")
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                repair_photo_path = filename
                
        if session.get('role') == 'tecnico' and status not in ['Observacion', 'Reparacion', 'Terminado']:
            flash('Un técnico solo puede cambiar el estado a Observación, Reparación o Terminado.', 'error')
            return redirect(url_for('view_device_details', device_id=device.id))

        try:
            new_repair = Repair(
                device_id=device.id,
                description=description,
                status=status,
                notes=notes,
                cost=float(cost) if cost else 0.0,
                price_to_customer=float(price_to_customer) if price_to_customer else 0.0,
                repair_photo_path=repair_photo_path,
            )
            
            if status == 'Terminado':
                new_repair.end_date = datetime.utcnow()

            db.session.add(new_repair)
            db.session.commit()
            
            device.current_status = status
            db.session.commit()
            
            flash('Reparación agregada exitosamente.', 'success')
            return redirect(url_for('view_device_details', device_id=device.id))
        except ValueError:
            db.session.rollback()
            flash('Costo y precio deben ser números válidos.', 'error')
        except Exception as e:
            db.session.rollback()
            flash(f'Error al agregar la reparación: {str(e)}', 'error')
            
    return render_template('add_repair.html', device=device)

@app.route('/admin/repair/<int:repair_id>/manage_components', methods=['GET', 'POST'])
@requires_roles('admin', 'tecnico')
def manage_components(repair_id):
    repair = Repair.query.get_or_404(repair_id)
    device = repair.device
    available_components = Component.query.filter(Component.stock_quantity > 0).all()
    if request.method == 'POST':
        component_id = request.form.get('component_id')
        quantity_used = request.form.get('quantity_used')
        if not component_id or not quantity_used:
            flash('Por favor, selecciona un componente y la cantidad.', 'warning')
            return redirect(url_for('manage_components', repair_id=repair.id))
        component = Component.query.get(int(component_id))
        if component and component.stock_quantity >= int(quantity_used):
            try:
                repair_component = RepairComponent.query.filter_by(
                    repair_id=repair.id, component_id=component.id
                ).first()

                if repair_component:
                    repair_component.quantity_used += int(quantity_used)
                else:
                    new_repair_component = RepairComponent(
                        repair_id=repair.id,
                        component_id=component.id,
                        quantity_used=int(quantity_used)
                    )
                    db.session.add(new_repair_component)

                component.stock_quantity -= int(quantity_used)
                db.session.add(component)
                costo_componente = component.price * int(quantity_used)
                repair.cost += costo_componente
                db.session.add(repair)
                db.session.commit()
                flash(f'{quantity_used} unidad(es) de {component.name} agregada(s) a la reparación. Costo actualizado.', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Error al agregar el componente: {str(e)}', 'error')
        else:
            flash('Stock insuficiente o componente no válido.', 'error')
    
    return render_template('manage_components.html', repair=repair, available_components=available_components)

@app.route('/admin/stock')
@requires_roles('admin', 'administrativo')
def manage_stock():
    components = Component.query.all()
    return render_template('manage_stock.html', components=components)

@app.route('/admin/add_component', methods=['POST'])
@requires_roles('admin', 'administrativo')
def add_component():
    name = request.form.get('name')
    stock_quantity = request.form.get('stock_quantity')
    price = request.form.get('price')
    if name and stock_quantity and price:
        try:
            new_component = Component(
                name=name,
                stock_quantity=int(stock_quantity),
                price=float(price)
            )
            db.session.add(new_component)
            db.session.commit()
            flash('Componente agregado exitosamente.', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error al agregar el componente: {str(e)}', 'error')
    else:
        flash('Faltan datos para agregar el componente.', 'warning')
    return redirect(url_for('manage_stock'))


if __name__ == '__main__':
    with app.app_context():
        if not os.path.exists(os.path.join(basedir, 'instance', 'site.db')):
            db.create_all()
            print("Base de datos creada.")

        # Lógica para desarrollo (se ejecuta solo si el archivo se corre directamente)
        if not User.query.filter_by(username='Admin').first():
            admin_user = User(username='Admin', role='admin')
            admin_user.set_password('')
            db.session.add(admin_user)
            print("Usuario 'Admin' creado para el entorno de desarrollo.")
            
        if not User.query.filter_by(username='vendedor').first():
            vendedor_user = User(username='vendedor', role='vendedor')
            vendedor_user.set_password('')
            db.session.add(vendedor_user)
            print("Usuario 'vendedor1' creado para desarrollo local.")
            
        if not User.query.filter_by(username='tecnico').first():
            tecnico_user = User(username='tecnico', role='tecnico')
            tecnico_user.set_password('')
            db.session.add(tecnico_user)
            print("Usuario 'tecnico' creado para desarrollo local.")
        
        if not User.query.filter_by(username='administrativo1').first():
            admin_user = User(username='administrativo1', role='administrativo')
            admin_user.set_password('')
            db.session.add(admin_user)
            print("Usuario 'administrativo1' creado para desarrollo local.")
        
        db.session.commit()
            
    app.run(debug=True)
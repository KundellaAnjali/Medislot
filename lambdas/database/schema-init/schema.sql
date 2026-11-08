CREATE TABLE IF NOT EXISTS users (
    user_id INT AUTO_INCREMENT PRIMARY KEY,

    email VARCHAR(255) UNIQUE NOT NULL,

    password_hash VARCHAR(255) NOT NULL,

    role ENUM(
        'DOCTOR',
        'PATIENT'
    ) NOT NULL,

    is_active BOOLEAN DEFAULT TRUE,

    last_login_at TIMESTAMP NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id INT AUTO_INCREMENT PRIMARY KEY,

    hospital_name VARCHAR(255) NOT NULL,

    city VARCHAR(100) NULL,

    address VARCHAR(500) NULL,

    phone_number VARCHAR(20) NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_hospitals_city (city)
);

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT NOT NULL UNIQUE,

    first_name VARCHAR(100) NULL,

    last_name VARCHAR(100) NULL,

    phone_number VARCHAR(20) NULL,

    license_number VARCHAR(100) UNIQUE NULL,

    experience_years INT NULL,

    qualification VARCHAR(255) NULL,

    profile_image_key VARCHAR(500) NULL,

    hospital_id INT NULL,

    languages VARCHAR(255) NULL,

    is_verified BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_doctor_user
        FOREIGN KEY (user_id)
        REFERENCES users(user_id),

    INDEX idx_doctors_hospital_id (hospital_id),

    CONSTRAINT fk_doctor_hospital
        FOREIGN KEY (hospital_id)
        REFERENCES hospitals(hospital_id)
);

CREATE TABLE IF NOT EXISTS patients (
    patient_id INT AUTO_INCREMENT PRIMARY KEY,

    user_id INT NOT NULL UNIQUE,

    first_name VARCHAR(100) NULL,

    last_name VARCHAR(100) NULL,

    phone_number VARCHAR(20) NULL,

    date_of_birth DATE NULL,

    gender VARCHAR(20) NULL,

    emergency_contact VARCHAR(20) NULL,
    emergency_contact_name VARCHAR(200) NULL,

    profile_filled BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_patient_user
        FOREIGN KEY (user_id)
        REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS specializations (
    specialization_id INT AUTO_INCREMENT PRIMARY KEY,

    specialization_name VARCHAR(255)
        NOT NULL UNIQUE,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doctor_specializations (

    doctor_id INT NOT NULL,

    specialization_id INT NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (
        doctor_id,
        specialization_id
    ),

    INDEX idx_doctor_specializations_specialization_doctor (
        specialization_id,
        doctor_id
    ),

    CONSTRAINT fk_ds_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors(doctor_id),

    CONSTRAINT fk_ds_specialization
        FOREIGN KEY (specialization_id)
        REFERENCES specializations(
            specialization_id
        )
);

CREATE TABLE IF NOT EXISTS consultation_types (

    consultation_type_id INT AUTO_INCREMENT PRIMARY KEY,

    consultation_name VARCHAR(255)
        NOT NULL,

    duration_minutes INT NOT NULL,

    capacity INT NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doctor_consulting_types (

    consultation_type_id INT NOT NULL,

    doctor_id INT NOT NULL,

    fee DECIMAL(10,2) NOT NULL,

    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    PRIMARY KEY (consultation_type_id, doctor_id),

    INDEX idx_doctor_consulting_types_doctor (doctor_id),

    CONSTRAINT fk_doctor_consulting_type
        FOREIGN KEY (consultation_type_id)
        REFERENCES consultation_types(consultation_type_id),

    CONSTRAINT fk_consulting_type_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors(doctor_id)
);

CREATE TABLE IF NOT EXISTS availability_patterns (

    pattern_id INT AUTO_INCREMENT PRIMARY KEY,

    doctor_id INT NOT NULL,

    consultation_type_id INT NOT NULL,

    day_of_week VARCHAR(20) NOT NULL,

    start_time TIME NOT NULL,

    end_time TIME NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_pattern_doctor_consulting_type
        FOREIGN KEY (consultation_type_id, doctor_id)
        REFERENCES doctor_consulting_types(consultation_type_id, doctor_id)
);

CREATE TABLE IF NOT EXISTS blackout_dates (

    blackout_id INT AUTO_INCREMENT PRIMARY KEY,

    doctor_id INT NOT NULL,

    start_datetime DATETIME NOT NULL,

    end_datetime DATETIME NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_blackout_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors(doctor_id)
);

CREATE TABLE IF NOT EXISTS appointments (

    appointment_id INT AUTO_INCREMENT PRIMARY KEY,

    patient_id INT
        NOT NULL,

    doctor_id INT
        NOT NULL,

    slot_id BIGINT
        NOT NULL,

    consultation_type_id INT
        NOT NULL,

    appointment_status VARCHAR(50)
        NOT NULL,

    appointment_datetime DATETIME
        NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    CONSTRAINT fk_appointment_patient
        FOREIGN KEY (patient_id)
        REFERENCES patients(patient_id),

    CONSTRAINT fk_appointment_doctor
        FOREIGN KEY (doctor_id)
        REFERENCES doctors(doctor_id),

    CONSTRAINT fk_appointment_consultation
        FOREIGN KEY (
            consultation_type_id
        )
        REFERENCES consultation_types(
            consultation_type_id
        )
);

CREATE TABLE IF NOT EXISTS payments (

    payment_id INT AUTO_INCREMENT PRIMARY KEY,

    appointment_id INT NOT NULL UNIQUE,

    amount DECIMAL(10,2) NOT NULL,

    transaction_id VARCHAR(255) NOT NULL,

    payment_method VARCHAR(50) NOT NULL,

    status VARCHAR(50) NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    paid_at TIMESTAMP NULL,

    CONSTRAINT fk_payment_appointment
        FOREIGN KEY (
            appointment_id
        )
        REFERENCES appointments(
            appointment_id
        )
);

CREATE TABLE IF NOT EXISTS refunds (

    refund_id INT AUTO_INCREMENT PRIMARY KEY,

    payment_id INT
        NOT NULL UNIQUE,

    refund_percentage DECIMAL(5,2)
        NOT NULL,

    refund_amount DECIMAL(10,2)
        NOT NULL,

    refund_transaction_id VARCHAR(255)
        NULL,

    status VARCHAR(50)
        NOT NULL,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    refunded_at TIMESTAMP NULL,

    CONSTRAINT fk_refund_payment
        FOREIGN KEY (payment_id)
        REFERENCES payments(
            payment_id
        )
);
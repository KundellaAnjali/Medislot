-- Synthetic, inactive doctor accounts for local or test databases only.
-- These accounts cannot log in while inactive. Replace the password value with
-- a bcrypt hash before explicitly activating an account.
-- All emails use the reserved example.test domain and all identities are fictitious.
-- Safe to re-run: users are matched by email and existing doctor rows are preserved.

START TRANSACTION;

INSERT INTO users (email, password_hash, role, is_active)
VALUES
    ('doctor01@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor02@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor03@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor04@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor05@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor06@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor07@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor08@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor09@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor10@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor11@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor12@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor13@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor14@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor15@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor16@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor17@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor18@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor19@example.test', 'password@123', 'DOCTOR', FALSE),
    ('doctor20@example.test', 'password@123', 'DOCTOR', FALSE)
ON DUPLICATE KEY UPDATE email = VALUES(email);

INSERT INTO doctors (
    user_id,
    first_name,
    last_name,
    phone_number,
    license_number,
    experience_years,
    qualification,
    hospital_id,
    languages,
    is_verified
)
SELECT
    u.user_id,
    seed.first_name,
    seed.last_name,
    seed.phone_number,
    seed.license_number,
    seed.experience_years,
    seed.qualification,
    NULL,
    seed.languages,
    FALSE
FROM (
    SELECT 'doctor01@example.test' AS email, 'Aarav' AS first_name, 'Mehta' AS last_name, '9000000001' AS phone_number, 'DEMO100001' AS license_number, 12 AS experience_years, 'MBBS, MD Cardiology' AS qualification, 'English, Hindi' AS languages
    UNION ALL SELECT 'doctor02@example.test', 'Ananya', 'Rao', '9000000002', 'DEMO100002', 8, 'MBBS, MD Dermatology', 'English, Telugu'
    UNION ALL SELECT 'doctor03@example.test', 'Vihaan', 'Shah', '9000000003', 'DEMO100003', 15, 'MBBS, MS Orthopedics', 'English, Gujarati'
    UNION ALL SELECT 'doctor04@example.test', 'Diya', 'Nair', '9000000004', 'DEMO100004', 6, 'MBBS, MD Pediatrics', 'English, Malayalam'
    UNION ALL SELECT 'doctor05@example.test', 'Arjun', 'Iyer', '9000000005', 'DEMO100005', 20, 'MBBS, MS General Surgery', 'English, Tamil'
    UNION ALL SELECT 'doctor06@example.test', 'Ishita', 'Kapoor', '9000000006', 'DEMO100006', 9, 'MBBS, MD Obstetrics and Gynecology', 'English, Hindi'
    UNION ALL SELECT 'doctor07@example.test', 'Reyansh', 'Patel', '9000000007', 'DEMO100007', 11, 'MBBS, MD Neurology', 'English, Gujarati'
    UNION ALL SELECT 'doctor08@example.test', 'Saanvi', 'Reddy', '9000000008', 'DEMO100008', 7, 'MBBS, MD Ophthalmology', 'English, Telugu'
    UNION ALL SELECT 'doctor09@example.test', 'Kabir', 'Joshi', '9000000009', 'DEMO100009', 18, 'MBBS, MD Psychiatry', 'English, Hindi'
    UNION ALL SELECT 'doctor10@example.test', 'Aadhya', 'Menon', '9000000010', 'DEMO100010', 5, 'MBBS, MD ENT', 'English, Malayalam'
    UNION ALL SELECT 'doctor11@example.test', 'Vivaan', 'Desai', '9000000011', 'DEMO100011', 14, 'MBBS, MD Gastroenterology', 'English, Gujarati'
    UNION ALL SELECT 'doctor12@example.test', 'Myra', 'Kulkarni', '9000000012', 'DEMO100012', 10, 'MBBS, MD Endocrinology', 'English, Marathi'
    UNION ALL SELECT 'doctor13@example.test', 'Advik', 'Sinha', '9000000013', 'DEMO100013', 16, 'MBBS, MD Pulmonology', 'English, Hindi'
    UNION ALL SELECT 'doctor14@example.test', 'Pari', 'Chawla', '9000000014', 'DEMO100014', 4, 'MBBS, MD Radiology', 'English, Punjabi'
    UNION ALL SELECT 'doctor15@example.test', 'Ishaan', 'Bose', '9000000015', 'DEMO100015', 13, 'MBBS, MD Nephrology', 'English, Bengali'
    UNION ALL SELECT 'doctor16@example.test', 'Sara', 'Thomas', '9000000016', 'DEMO100016', 9, 'MBBS, MD Oncology', 'English, Malayalam'
    UNION ALL SELECT 'doctor17@example.test', 'Dhruv', 'Malhotra', '9000000017', 'DEMO100017', 17, 'MBBS, MD Urology', 'English, Hindi'
    UNION ALL SELECT 'doctor18@example.test', 'Kiara', 'Pillai', '9000000018', 'DEMO100018', 6, 'MBBS, MD Rheumatology', 'English, Tamil'
    UNION ALL SELECT 'doctor19@example.test', 'Ritvik', 'Mishra', '9000000019', 'DEMO100019', 21, 'MBBS, MD Oncology', 'English, Hindi'
    UNION ALL SELECT 'doctor20@example.test', 'Navya', 'Fernandes', '9000000020', 'DEMO100020', 8, 'MBBS, MD Family Medicine', 'English, Konkani'
) AS seed
JOIN users AS u
  ON u.email = seed.email
 AND u.role = 'DOCTOR'
LEFT JOIN doctors AS existing_doctor
  ON existing_doctor.user_id = u.user_id
WHERE existing_doctor.doctor_id IS NULL;

COMMIT;

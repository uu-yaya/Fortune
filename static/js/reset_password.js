const phoneInput = document.getElementById("phoneInput");
const codeInput = document.getElementById("codeInput");
const sendCodeBtn = document.getElementById("sendCodeBtn");
const newPasswordInput = document.getElementById("newPasswordInput");
const confirmPasswordInput = document.getElementById("confirmPasswordInput");
const resetBtn = document.getElementById("resetBtn");
const statusText = document.getElementById("statusText");

let countdown = 0;
let timer = null;

function setStatus(msg, isError = false) {
    statusText.textContent = msg || "";
    statusText.classList.toggle("error", !!isError);
}

function normalizePhone(v) {
    return String(v || "").replace(/\s+/g, "");
}

function validPhone(v) {
    return /^1\d{10}$/.test(v);
}

function validPassword(v) {
    return /^[A-Za-z0-9]{8,12}$/.test(String(v || ""));
}

function startCountdown(sec) {
    countdown = sec;
    if (timer) clearInterval(timer);
    sendCodeBtn.disabled = true;
    sendCodeBtn.textContent = `${countdown}s`;
    timer = setInterval(() => {
        countdown -= 1;
        if (countdown <= 0) {
            clearInterval(timer);
            timer = null;
            sendCodeBtn.disabled = false;
            sendCodeBtn.textContent = "发送验证码";
            return;
        }
        sendCodeBtn.textContent = `${countdown}s`;
    }, 1000);
}

async function sendCode() {
    const phone = normalizePhone(phoneInput.value);
    if (!validPhone(phone)) {
        setStatus("请输入有效的11位手机号", true);
        return;
    }
    sendCodeBtn.disabled = true;
    setStatus("正在发送验证码...");
    try {
        const resp = await fetch("/auth/send_code", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ phone, scene: "reset_password" })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
            setStatus(data.message || "发送失败，请稍后重试", true);
            sendCodeBtn.disabled = false;
            return;
        }
        if (data.debug_code) {
            setStatus(`验证码已发送：${data.debug_code}（演示）`);
        } else {
            setStatus("验证码已发送，请注意查收短信。");
        }
        startCountdown(data.ttl_seconds || 60);
    } catch (e) {
        setStatus("发送失败，请检查网络后重试", true);
        sendCodeBtn.disabled = false;
    }
}

async function resetPassword() {
    const phone = normalizePhone(phoneInput.value);
    const code = String(codeInput.value || "").trim();
    const newPassword = String(newPasswordInput.value || "").trim();
    const confirmPassword = String(confirmPasswordInput.value || "").trim();
    if (!validPhone(phone)) {
        setStatus("请输入有效的11位手机号", true);
        return;
    }
    if (!/^\d{6}$/.test(code)) {
        setStatus("请输入6位验证码", true);
        return;
    }
    if (!validPassword(newPassword)) {
        setStatus("密码需为8-12位字母或数字", true);
        return;
    }
    if (newPassword !== confirmPassword) {
        setStatus("两次输入的密码不一致", true);
        return;
    }
    setStatus("正在校验验证码...");
    try {
        const verifyResp = await fetch("/auth/password/verify_code", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ phone, code })
        });
        const verifyData = await verifyResp.json();
        if (!verifyResp.ok || !verifyData.ok) {
            setStatus(verifyData.message || "验证码校验失败", true);
            return;
        }

        setStatus("正在重置密码...");
        const resp = await fetch("/auth/password/reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ phone, new_password: newPassword, confirm_password: confirmPassword })
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
            setStatus(data.message || "重置失败", true);
            return;
        }
        setStatus("密码重置成功，正在跳转登录页...");
        setTimeout(() => {
            window.location.href = "/login";
        }, 900);
    } catch (e) {
        setStatus("请求失败，请稍后重试", true);
    }
}

sendCodeBtn.addEventListener("click", sendCode);
resetBtn.addEventListener("click", resetPassword);

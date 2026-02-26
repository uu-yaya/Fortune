// DOM元素
const menuItems = document.querySelectorAll('.menu-item');
const containers = {
    chat: document.querySelector('.chat-container'),
    bazi: document.querySelector('.bazi-container'),
    fortune: document.querySelector('.fortune-container'),
    dream: document.querySelector('.dream-container'),
    divination: document.querySelector('.divination-container')
};
const chatMessages = document.getElementById('chatMessages');
const userInput = document.getElementById('userInput');
const sendButton = document.getElementById('sendButton');
const logoutBtn = document.getElementById('logoutBtn');
// 已改为占位气泡提示，禁用全屏蒙层加载效果
const loadingOverlay = null;
const TYPING_INTERVAL_MS = 16;
const SUGGESTION_STATE_KEY = 'jiyi_suggestion_state_v2';
const SUGGESTION_RECENT_KEY = 'jiyi_suggestion_recent_v1';
const DEEP_EMOTION_KEYWORDS = /(撑不住|难过|放弃|崩溃|委屈|压抑|痛苦|绝望|失眠|焦虑|不想活|活不下去|没意义)/;
const SUGGESTION_LIBRARY_POSITIVE = {
    emotion: {
        light: [
            '我最近想让自己开心一点，可以从哪里开始？',
            '我现在的状态，其实已经在慢慢变好吗？',
            '我今天可以为自己做一件小小的好事吗？',
            '我现在最需要补充的能量是什么？'
        ],
        medium: [
            '我该怎么让自己更轻松一点？',
            '我可以换一个角度看待现在的困扰吗？',
            '我现在的难受，是在提醒我什么？',
            '我有哪些已经做得不错的地方？'
        ],
        deep: [
            '如果我选择相信自己，会发生什么改变？',
            '我是不是已经比过去勇敢很多了？',
            '这段经历，会让我成长成什么样子？',
            '我现在可以给自己一个怎样的新开始？'
        ],
        hooks: [
            '我是不是已经悄悄进步了，只是自己没发现？',
            '如果我对自己温柔一点，会不会轻松很多？',
            '我现在最值得被肯定的地方是什么？',
            '这段时间，是不是在为更好的阶段做准备？'
        ]
    },
    love: {
        light: [
            '我该怎么更自然地表达喜欢？',
            '我在这段关系里，有没有被珍惜？',
            '我可以更轻松地相处吗？',
            '这段感情带给我什么好的改变？'
        ],
        medium: [
            '我该怎么在爱里保护自己？',
            '我适合怎样的相处方式？',
            '我是不是可以更主动表达真实想法？',
            '我可以把期待说出来吗？'
        ],
        deep: [
            '我怎样才能遇到真正坚定的人？',
            '我该如何成为更自在的自己？',
            '我现在的感情状态，是在帮我成长吗？',
            '我是不是正在靠近更适合我的关系？'
        ],
        hooks: [
            '我什么时候会被认真而坚定地选择？',
            '我是不是值得一段安心的关系？',
            '我可以怎样让爱变得更轻松？',
            '这段关系教会了我什么？'
        ]
    },
    work: {
        light: [
            '我现在的努力，会慢慢开花吗？',
            '我今天可以完成哪一小步？',
            '我适合什么样的节奏？',
            '我有哪些优势还没被发挥？'
        ],
        medium: [
            '我该往哪个方向多走一步？',
            '我是不是可以尝试新的方式？',
            '我可以怎样减少内耗？',
            '我现在最值得坚持的是什么？'
        ],
        deep: [
            '我真正擅长的领域在哪里？',
            '如果我大胆一点，会发生什么？',
            '我适合更大的舞台吗？',
            '我现在是在积累阶段吗？'
        ],
        hooks: [
            '我是不是已经比想象中更接近目标？',
            '这段低谷是不是在帮我沉淀实力？',
            '我是不是低估了自己的潜力？',
            '我该怎么更自信地展示自己？'
        ]
    },
    self: {
        light: [
            '我最近有哪些值得夸夸自己的地方？',
            '我可以怎么更爱自己一点？',
            '我现在的选择，是在对自己负责吗？',
            '我想成为一个什么样的人？'
        ],
        medium: [
            '我是不是可以更坚定地表达边界？',
            '我真正重视的价值是什么？',
            '我该如何减少对他人评价的焦虑？',
            '我现在的困惑，是在提醒我转变吗？'
        ],
        deep: [
            '如果我完全相信自己，会发生什么？',
            '我未来三个月可以成长在哪个方向？',
            '我适合怎样的人生节奏？',
            '我该放下什么，才能更自由？'
        ],
        hooks: [
            '我是不是已经在变成更好的自己？',
            '我现在最需要听到的一句话是什么？',
            '我是不是可以对自己更有耐心？',
            '我是不是值得为自己勇敢一次？'
        ]
    },
    fortune: {
        light: [
            '我今天整体运势最该注意什么？',
            '现在更适合主动推进还是稳住节奏？',
            '我最近财运是开源优先还是守财优先？',
            '我这周感情运里最关键的一步是什么？'
        ],
        medium: [
            '结合我的命理线索，给我三条具体行动建议。',
            '我最近事业运的阻力点和助力点分别是什么？',
            '我这段时间做决策，应该先看风险还是先抓机会？',
            '如果只做一件最能提运的小事，应该做什么？'
        ],
        deep: [
            '请按命理依据拆解：结论、风险点、时间窗口。',
            '给我看最近一个月的运势节奏，分上中下旬说。',
            '我目前五行失衡最明显的地方是什么，如何补？',
            '我未来三个月在事业和财运上该如何排优先级？'
        ],
        hooks: [
            '我这段运势里最该“避坑”的一个动作是什么？',
            '我最近最容易错过的机会会出现在什么场景？',
            '我现在适合冲刺还是蓄力，给一个明确判断。',
            '我今天最旺的时间段和最该收敛的时间段是什么？'
        ]
    },
    universal_explore: [
        '我现在最值得优先处理的是什么？',
        '我可以从哪件小事开始改变？',
        '我接下来一个月的主题是什么？',
        '现在这段经历，对我未来有什么意义？',
        '我该把注意力放在哪里？',
        '我是不是正在走向对的方向？'
    ]
};

// 会话由后端按登录用户UUID维护，前端不再依赖 localStorage 的 session_id
let session_id = '';
let profileCache = { name: '', birthdate: '' };
let isSending = false;

let suggestionState = loadSuggestionState();

function getProfile() {
    if (!isValidName(profileCache.name)) {
        profileCache.name = '';
    }
    return { ...profileCache };
}

function saveProfile(profile) {
    profileCache = {
        name: String(profile?.name || ''),
        birthdate: String(profile?.birthdate || '')
    };
}

function normalizeBirthdate(text) {
    const match = String(text || '').match(/(\d{4})[\/\-.年]\s*(\d{1,2})[\/\-.月]\s*(\d{1,2})/);
    if (!match) return '';
    const y = match[1];
    const m = match[2].padStart(2, '0');
    const d = match[3].padStart(2, '0');
    return `${y}-${m}-${d}`;
}

function isValidName(name) {
    const n = String(name || '').trim();
    if (!n) return false;
    if (n.length < 2 || n.length > 12) return false;
    // 明确过滤疑问词和代词，避免把“我是谁”之类写入姓名
    if (/(^我$|^谁$|什么|咋|怎么|为何|吗$|呢$|呀$|啊$|\?|？)/.test(n)) return false;
    return true;
}

function extractNameFromText(sourceText) {
    const source = String(sourceText || '').trim();
    // 疑问句直接不做姓名提取
    if (/[？?]/.test(source) || /(我是谁|你猜|叫什么|名字是啥|名字是什么)/.test(source)) {
        return '';
    }
    // 仅接受更明确的“报姓名”句式，避免“我是...”误判
    const patterns = [
        /(?:我叫|名字是|姓名是|我的名字是)\s*([^\s，。！？,.]{2,12})/,
        /^(?:姓名|名字)\s*[:：]\s*([^\s，。！？,.]{2,12})$/
    ];
    for (const p of patterns) {
        const m = source.match(p);
        if (m && isValidName(m[1])) {
            return m[1].trim();
        }
    }
    return '';
}

function updateProfileFromText(text) {
    const source = String(text || '');
    const profile = getProfile();
    let changed = false;

    const extractedName = extractNameFromText(source);
    if (!profile.name && extractedName) {
        profile.name = extractedName;
        changed = true;
    }

    const birthdate = normalizeBirthdate(source);
    if (!profile.birthdate && birthdate) {
        profile.birthdate = birthdate;
        changed = true;
    }

    if (changed) saveProfile(profile);
    return profile;
}

async function bootstrapProfileFromServer() {
    try {
        const resp = await fetch('/auth/me', { credentials: 'same-origin' });
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data?.ok) return;
        const p = data.profile || {};
        saveProfile({
            name: isValidName(p.name) ? p.name : '',
            birthdate: normalizeBirthdate(p.birthdate || '')
        });
    } catch (e) {
        // 忽略初始化失败，后续聊天会继续由后端提取资料
    }
}

function setSendBusyState(busy) {
    isSending = busy;
    if (!sendButton) return;
    sendButton.disabled = busy;
    sendButton.classList.toggle('is-busy', busy);
}

async function parseJsonSafely(response) {
    const contentType = (response.headers.get('content-type') || '').toLowerCase();
    if (contentType.includes('application/json')) {
        return response.json();
    }
    const text = await response.text();
    throw new Error(`NON_JSON_RESPONSE:${response.status}:${text.slice(0, 120)}`);
}

function getChatOutput(data) {
    if (typeof data === 'object' && data !== null) {
        if (typeof data.output === 'string') return data.output;
        if (typeof data.data === 'string') return data.data;
        return JSON.stringify(data, null, 2);
    }
    return String(data ?? '');
}

function pickRandom(items, count) {
    const list = [...new Set(items)];
    for (let i = list.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [list[i], list[j]] = [list[j], list[i]];
    }
    return list.slice(0, count);
}

function loadSuggestionState() {
    try {
        const raw = localStorage.getItem(SUGGESTION_STATE_KEY);
        const parsed = raw ? JSON.parse(raw) : {};
        return {
            consecutiveClicks: Number(parsed.consecutiveClicks || 0),
            lastClickedText: String(parsed.lastClickedText || '')
        };
    } catch (e) {
        return { consecutiveClicks: 0, lastClickedText: '' };
    }
}

function saveSuggestionState() {
    localStorage.setItem(SUGGESTION_STATE_KEY, JSON.stringify(suggestionState));
}

function loadRecentSuggestions() {
    try {
        const raw = localStorage.getItem(SUGGESTION_RECENT_KEY);
        const arr = raw ? JSON.parse(raw) : [];
        return Array.isArray(arr) ? arr.slice(-30) : [];
    } catch (e) {
        return [];
    }
}

function saveRecentSuggestions(items) {
    localStorage.setItem(SUGGESTION_RECENT_KEY, JSON.stringify((items || []).slice(-30)));
}

function detectCategory(text) {
    const q = String(text || '');
    if (/(算命|八字|流年|命理|命盘|运势|占卜|卦象|摇卦|测算|财运|事业运|学业运)/.test(q)) return 'fortune';
    if (/(恋爱|感情|前任|暧昧|喜欢|对象|桃花|分手|复合)/.test(q)) return 'love';
    if (/(工作|事业|学习|考试|面试|升职|职场|offer|跳槽)/.test(q)) return 'work';
    if (/(害怕|焦虑|情绪|压力|失眠|难过|委屈|勇敢|自己|成长|自信|安全感)/.test(q)) return 'self';
    return 'emotion';
}

function resolveSuggestionLevel(lastQuery, deepTriggered) {
    if (deepTriggered) return 'deep';
    if (suggestionState.consecutiveClicks >= 2) {
        return 'medium';
    }
    return 'light';
}

function pickWithRecencyAvoid(pool, count, recentItems) {
    const uniquePool = [...new Set(pool)];
    const recentSet = new Set(recentItems || []);
    const fresh = uniquePool.filter((it) => !recentSet.has(it));
    const primary = pickRandom(fresh, count);
    if (primary.length >= count) return primary;
    const fallback = pickRandom(uniquePool.filter((it) => !primary.includes(it)), count - primary.length);
    return primary.concat(fallback);
}

function buildFollowupSuggestions(lastQuery, options = {}) {
    const { deepTriggered = false, homeMode = false } = options;
    const category = detectCategory(lastQuery);
    const bucket = SUGGESTION_LIBRARY_POSITIVE[category] || SUGGESTION_LIBRARY_POSITIVE.emotion;
    const level = homeMode ? 'light' : resolveSuggestionLevel(lastQuery, deepTriggered);
    const result = [];
    const recent = loadRecentSuggestions();
    const fromLevel = pickWithRecencyAvoid(bucket[level] || bucket.light || [], 3, recent);
    const hooksPool = category === 'fortune'
        ? [...(bucket.hooks || [])]
        : [...(bucket.hooks || []), ...SUGGESTION_LIBRARY_POSITIVE.universal_explore];
    const fromHooks = pickWithRecencyAvoid(hooksPool, homeMode ? 2 : 1, recent);

    result.push(...fromLevel, ...fromHooks);
    const finalList = pickRandom(result, 4);
    saveRecentSuggestions([...recent, ...finalList]);

    return finalList.map((text) => ({
        text,
        isHook: hooksPool.includes(text)
    }));
}

function isDeepEmotionTriggered(query, fromSuggestionClick) {
    if (fromSuggestionClick) return false;
    return DEEP_EMOTION_KEYWORDS.test(String(query || ''));
}

function withProfileHintIfMissing(text) {
    return String(text || "");
}

function appendSuggestionButtons(anchorMessageEl, suggestions) {
    if (!chatMessages || !anchorMessageEl || !Array.isArray(suggestions) || suggestions.length === 0) return;

    const wrap = document.createElement('div');
    wrap.className = 'quick-suggestions';
    suggestions.slice(0, 4).forEach((item) => {
        const text = typeof item === 'string' ? item : item.text;
        const isHook = typeof item === 'object' && !!item.isHook;
        if (!text) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `quick-suggestion-btn ${isHook ? 'is-hook' : ''}`;
        btn.textContent = text;
        btn.addEventListener('click', () => {
            if (!userInput) return;
            userInput.value = text;
            userInput.focus();
            suggestionState.consecutiveClicks = Math.min(suggestionState.consecutiveClicks + 1, 6);
            suggestionState.lastClickedText = text;
            saveSuggestionState();
        });
        wrap.appendChild(btn);
    });

    anchorMessageEl.insertAdjacentElement('afterend', wrap);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function playHeartEffect(button) {
    if (!button) return;

    button.classList.remove('heart-pop');
    void button.offsetWidth;
    button.classList.add('heart-pop');

    const hearts = ['❤', '♡', '✦', '❀'];
    for (let i = 0; i < 7; i++) {
        const heart = document.createElement('span');
        heart.className = 'heart-float';
        heart.textContent = hearts[Math.floor(Math.random() * hearts.length)];

        const dx = `${(Math.random() - 0.5) * 80}px`;
        const dy = `${-30 - Math.random() * 70}px`;
        heart.style.setProperty('--dx', dx);
        heart.style.setProperty('--dy', dy);
        heart.style.fontSize = `${12 + Math.floor(Math.random() * 9)}px`;

        button.appendChild(heart);
        setTimeout(() => heart.remove(), 1000);
    }

    setTimeout(() => button.classList.remove('heart-pop'), 360);
}

// 切换菜单
if (menuItems.length > 0) {
    menuItems.forEach(item => {
        item.addEventListener('click', () => {
            // 移除所有active类
            menuItems.forEach(i => i.classList.remove('active'));
            // 添加active类到当前项
            item.classList.add('active');

            // 隐藏所有容器
            Object.values(containers).forEach(container => {
                if (container) {
                    container.classList.add('hidden');
                }
            });

            // 显示选中的容器
            const type = item.dataset.type;
            if (containers[type]) {
                containers[type].classList.remove('hidden');
            }
        });
    });
}

// 发送消息
async function sendMessage() {
    if (!userInput || !chatMessages) return;
    if (isSending) return;
    const message = userInput.value.trim();
    if (!message) return;
    const fromSuggestionClick = suggestionState.lastClickedText && suggestionState.lastClickedText === message;
    if (!fromSuggestionClick) {
        suggestionState.consecutiveClicks = 0;
        suggestionState.lastClickedText = '';
        saveSuggestionState();
    }
    const deepTriggered = isDeepEmotionTriggered(message, fromSuggestionClick);

    playHeartEffect(sendButton);
    updateProfileFromText(message);

    // 添加用户消息到聊天界面
    addMessage(message, 'user');
    userInput.value = '';
    const thinkingMessage = addThinkingMessage();

    setSendBusyState(true);

    try {
        // 发送请求到后端
        const response = await fetch('/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ query: message, session_id })
        });
        if (!response.ok) {
            const bodyText = await response.text();
            throw new Error(`HTTP_${response.status}:${bodyText.slice(0, 120)}`);
        }
        const data = await parseJsonSafely(response);
        const botResponse = getChatOutput(data);

        await replaceWithTypingEffect(thinkingMessage, withProfileHintIfMissing(botResponse));
        appendSuggestionButtons(thinkingMessage, buildFollowupSuggestions(message, { deepTriggered }));
    } catch (error) {
        console.error('Error:', error);
        await replaceWithTypingEffect(thinkingMessage, '呜啦…网络刚刚有点抖，本鼠鼠还在这儿。请再发一次试试呀。');
        appendSuggestionButtons(thinkingMessage, buildFollowupSuggestions(message, { deepTriggered }));
    } finally {
        setSendBusyState(false);
    }
}

// 添加消息到聊天界面
function addMessage(text, type) {
    if (!chatMessages) return;
    const messageDiv = document.createElement('div');
    messageDiv.classList.add('message', `${type}-message`);
    
    // 处理文本中的换行符
    const formattedText = text.replace(/\n/g, '<br>');
    messageDiv.innerHTML = formattedText;
    
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function addThinkingMessage() {
    if (!chatMessages) return null;
    const messageDiv = document.createElement('div');
    messageDiv.classList.add('message', 'bot-message', 'thinking-message');
    messageDiv.innerHTML = '吉伊正在思考<span class="thinking-dots"><i></i><i></i><i></i></span>';
    chatMessages.appendChild(messageDiv);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return messageDiv;
}

async function replaceWithTypingEffect(messageEl, fullText) {
    if (!messageEl) {
        addMessage(fullText, 'bot');
        return;
    }

    messageEl.classList.remove('thinking-message');
    messageEl.innerHTML = '';

    const text = typeof fullText === 'string' ? fullText : String(fullText || '');
    for (let i = 1; i <= text.length; i++) {
        const partial = text.slice(0, i).replace(/\n/g, '<br>');
        messageEl.innerHTML = partial;
        if (chatMessages) chatMessages.scrollTop = chatMessages.scrollHeight;
        await new Promise(resolve => setTimeout(resolve, TYPING_INTERVAL_MS));
    }
}

// 发送按钮点击事件
if (sendButton) {
    sendButton.addEventListener('click', sendMessage);
}

// 输入框回车事件
if (userInput) {
    userInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
}

if (chatMessages) {
    const initialBot = chatMessages.querySelector('.message.bot-message');
    if (initialBot) {
        const initSuggestions = buildFollowupSuggestions('最近有点迷糊', { homeMode: true });
        appendSuggestionButtons(initialBot, initSuggestions);
    }
}

bootstrapProfileFromServer();

if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
        try {
            await fetch('/auth/logout', { method: 'POST' });
        } catch (e) {
            // ignore
        } finally {
            window.location.href = '/login';
        }
    });
}

// 八字测算表单提交
const baziForm = document.getElementById('baziForm');
if (baziForm) {
    baziForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const data = {
        name: formData.get('name'),
        sex: formData.get('sex'),
        birthdate: formData.get('birthdate'),
        birthtime: formData.get('birthtime')
    };

    if (loadingOverlay) {
        loadingOverlay.classList.remove('hidden');
    }

    try {
        const response = await fetch('/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                query: `请帮我测算八字，姓名：${data.name}，性别：${data.sex === '0' ? '男' : '女'}，出生日期：${data.birthdate}，出生时间：${data.birthtime}`,
                session_id
            })
        });

        if (!response.ok) {
            throw new Error(`HTTP_${response.status}`);
        }
        const result = await parseJsonSafely(response);
        addMessage(getChatOutput(result), 'bot');
    } catch (error) {
        console.error('Error:', error);
        addMessage('抱歉，测算过程中发生错误，请稍后再试。', 'bot');
    } finally {
        if (loadingOverlay) {
            loadingOverlay.classList.add('hidden');
        }
    }
    });
}

// 解梦表单提交
const dreamForm = document.getElementById('dreamForm');
if (dreamForm) {
    dreamForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const dream = formData.get('dream');

    if (loadingOverlay) {
        loadingOverlay.classList.remove('hidden');
    }

    try {
        const response = await fetch('/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                query: `请帮我解梦，我的梦境是：${dream}`,
                session_id
            })
        });

        if (!response.ok) {
            throw new Error(`HTTP_${response.status}`);
        }
        const result = await parseJsonSafely(response);
        addMessage(getChatOutput(result), 'bot');
    } catch (error) {
        console.error('Error:', error);
        addMessage('抱歉，解梦过程中发生错误，请稍后再试。', 'bot');
    } finally {
        if (loadingOverlay) {
            loadingOverlay.classList.add('hidden');
        }
    }
    });
}

// 摇卦占卜
const startDivination = document.getElementById('startDivination');
if (startDivination) {
    startDivination.addEventListener('click', async () => {
    if (loadingOverlay) {
        loadingOverlay.classList.remove('hidden');
    }

    try {
        const response = await fetch('/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                query: '请帮我摇一卦',
                session_id
            })
        });

        if (!response.ok) {
            throw new Error(`HTTP_${response.status}`);
        }
        const result = await parseJsonSafely(response);
        const divinationResult = document.getElementById('divinationResult');
        if (divinationResult) {
            divinationResult.classList.remove('hidden');
            divinationResult.innerHTML = `<p>${getChatOutput(result)}</p>`;
        } else {
            addMessage(getChatOutput(result), 'bot');
        }
    } catch (error) {
        console.error('Error:', error);
        addMessage('抱歉，占卜过程中发生错误，请稍后再试。', 'bot');
    } finally {
        if (loadingOverlay) {
            loadingOverlay.classList.add('hidden');
        }
    }
    });
}

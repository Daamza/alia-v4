;(function(){
  // 1) Inyectar CSS (si lo tuvieras en static/chat.css)
  const css = document.createElement('link');
  css.rel = 'stylesheet';
  css.href = '/static/chat.css';
  document.head.appendChild(css);

  // 2) Launcher flotante
  const launcher = document.createElement('div');
  launcher.id = 'alia-launcher';
  Object.assign(launcher.style, {
    position: 'fixed', bottom: '20px', right: '20px',
    width: '60px', height: '60px', background: '#4CAF50',
    borderRadius: '50%', cursor: 'pointer', zIndex: 99999,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    color: '#fff', fontSize: '28px', userSelect: 'none'
  });
  launcher.textContent = '🤖';
  document.body.appendChild(launcher);

  // 3) Contenedor del chat (oculto)
  const chatContainer = document.createElement('div');
  chatContainer.id = 'alia-iframe-container';
  Object.assign(chatContainer.style, {
    position: 'fixed', bottom: '100px', right: '20px',
    width: '350px', height: '500px',
    border: '1px solid #ccc', borderRadius: '8px',
    boxShadow: '0 4px 16px rgba(0,0,0,0.2)',
    display: 'none', zIndex: 99998, overflow: 'hidden'
  });
  const iframe = document.createElement('iframe');
  const session = localStorage.getItem('ALIA_sessionId') || (()=>{
    const s = crypto.randomUUID();
    localStorage.setItem('ALIA_sessionId', s);
    return s;
  })();
  iframe.src = `/chat?session=${session}`;
  iframe.style.cssText = 'width:100%;height:100%;border:none;';
  chatContainer.appendChild(iframe);
  document.body.appendChild(chatContainer);

  // 4) Toggle
  launcher.addEventListener('click', ()=>{
    chatContainer.style.display =
      chatContainer.style.display === 'none' ? 'block' : 'none';
  });
})();

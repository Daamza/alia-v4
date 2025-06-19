;(function(){
  // 1) Inyectar CSS de tu chat
  const css = document.createElement('link');
  css.rel = 'stylesheet';
  css.href = '/static/chat.css';
  document.head.appendChild(css);

  // 2) Launcher flotante que ahora contiene tu logo
  const launcher = document.createElement('div');
  launcher.id = 'alia-launcher';
  Object.assign(launcher.style, {
    position:       'fixed',
    bottom:         '20px',
    right:          '20px',
    width:          '60px',
    height:         '60px',
    background:     '#fff',           // fondo blanco para el contorno
    borderRadius:   '50%',
    cursor:         'pointer',
    zIndex:         99999,
    display:        'flex',
    alignItems:     'center',
    justifyContent: 'center',
    boxShadow:      '0 2px 8px rgba(0,0,0,0.2)',
    overflow:       'hidden'
  });

  // 2a) imagen del logo
  const icon = document.createElement('img');
  icon.src = '/static/alia-icon.png';   // <-- tu logo
  icon.alt = 'ALIA';
  Object.assign(icon.style, {
    width:  '80%',
    height: '80%',
    objectFit: 'contain'
  });
  launcher.appendChild(icon);
  document.body.appendChild(launcher);

  // 3) Contenedor del chat (oculto inicialmente)
  const chatContainer = document.createElement('div');
  chatContainer.id = 'alia-iframe-container';
  Object.assign(chatContainer.style, {
    position:      'fixed',
    bottom:        '100px',
    right:         '20px',
    width:         '350px',
    height:        '500px',
    border:        '1px solid #ccc',
    borderRadius:  '8px',
    boxShadow:     '0 4px 16px rgba(0,0,0,0.2)',
    display:       'none',
    zIndex:        99998,
    overflow:      'hidden'
  });

  // 3a) iframe con tu chat
  const iframe = document.createElement('iframe');
  const session = localStorage.getItem('ALIA_sessionId') || (() => {
    const s = crypto.randomUUID();
    localStorage.setItem('ALIA_sessionId', s);
    return s;
  })();
  iframe.src = `/chat?session=${session}`;
  iframe.style.cssText = 'width:100%;height:100%;border:none;';
  chatContainer.appendChild(iframe);
  document.body.appendChild(chatContainer);

  // 4) Toggle on/off al click en el launcher
  launcher.addEventListener('click', () => {
    chatContainer.style.display =
      chatContainer.style.display === 'none' ? 'block' : 'none';
  });
})();

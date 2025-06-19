;(function(){
  // 1) Inyectar CSS de tu chat
  const css = document.createElement('link');
  css.rel = 'stylesheet';
  css.href = '/static/chat.css';    // <-- asegúrate de que exista este archivo
  document.head.appendChild(css);

  // 2) Launcher flotante
  const launcher = document.createElement('div');
  launcher.id = 'alia-launcher';
  Object.assign(launcher.style, {
    position:      'fixed',
    bottom:        '20px',
    right:         '20px',
    width:         '60px',
    height:        '60px',
    background:    '#4CAF50',
    borderRadius:  '50%',
    cursor:        'pointer',
    zIndex:        99999,
    display:       'flex',
    alignItems:    'center',
    justifyContent:'center',
    color:         '#fff',
    fontSize:      '28px',
    userSelect:    'none'
  });
  launcher.textContent = '🤖';
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

  // 3a) Crear el iframe que cargará tu chat.html
  const iframe = document.createElement('iframe');
  // Generar o recuperar sessionId
  const session = localStorage.getItem('ALIA_sessionId') || (() => {
    const s = crypto.randomUUID();
    localStorage.setItem('ALIA_sessionId', s);
    return s;
  })();
  iframe.src = `/chat?session=${session}`;
  iframe.style.cssText = 'width:100%;height:100%;border:none;';
  chatContainer.appendChild(iframe);
  document.body.appendChild(chatContainer);

  // 4) Toggle on/off al clicar en el launcher
  launcher.addEventListener('click', () => {
    chatContainer.style.display =
      chatContainer.style.display === 'none' ? 'block' : 'none';
  });
})();

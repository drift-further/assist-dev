// notifications.js — Top-slide toast notification system

const _toastContainer = document.createElement('div');
_toastContainer.className = 'toast-container';
_toastContainer.id = 'toast-container';
document.body.appendChild(_toastContainer);

function _getMaxToasts() { return SETTINGS ? SETTINGS.ui.max_toasts : 3; }
function _getToastDismiss() { return SETTINGS ? SETTINGS.ui.toast_duration_ms : 8000; }

function showToast(message, type, persistent, onTap) {
    type = type || 'cyan';
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;

    // Swipe right to dismiss
    let startX = 0;
    toast.addEventListener('touchstart', e => { startX = e.touches[0].clientX; }, {passive: true});
    toast.addEventListener('touchend', e => {
        const dx = e.changedTouches[0].clientX - startX;
        if (dx > 60) {
            toast.classList.add('swiped');
            setTimeout(() => toast.remove(), 300);
        }
    }, {passive: true});

    // Tap to invoke callback
    if (onTap) {
        toast.addEventListener('click', () => {
            onTap();
            toast.classList.add('swiped');
            setTimeout(() => toast.remove(), 300);
        });
    }

    _toastContainer.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('visible'));

    // Limit visible toasts
    while (_toastContainer.children.length > _getMaxToasts()) {
        _toastContainer.removeChild(_toastContainer.firstChild);
    }

    // Auto-dismiss (unless persistent)
    if (!persistent) {
        setTimeout(() => {
            if (toast.parentNode) {
                toast.classList.remove('visible');
                setTimeout(() => toast.remove(), 300);
            }
        }, _getToastDismiss());
    }
}

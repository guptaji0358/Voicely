#include "DialogFrame.h"

#include "Buttons.h"
#include "Controls.h"
#include "Frameless.h"
#include "Icons.h"
#include "Indicators.h"
#include "ThemeManager.h"

#include <QApplication>
#include <QHBoxLayout>
#include <QLabel>
#include <QLinearGradient>
#include <QMouseEvent>
#include <QPainter>
#include <QVBoxLayout>
#include <QWindow>

// ------------------------------------------------------------------------------------------- DialogTitleBar
DialogTitleBar::DialogTitleBar(QWidget *parent) : QWidget(parent) {
    setObjectName("dialogTitleBar");
    auto *h = new QHBoxLayout(this);
    h->setContentsMargins(16, 0, 0, 0);
    h->setSpacing(10);
    m_icon = new QLabel;
    m_icon->setFixedSize(20, 20);
    m_title = new QLabel;
    m_title->setObjectName("appName");
    m_close = new AnimatedButton;
    m_close->setObjectName("dialogClose");
    m_close->setProperty("variant", "caption");
    m_close->setIcon(Icons::icon("window-close"));
    m_close->setIconSize(QSize(14, 14));
    m_close->setFocusPolicy(Qt::NoFocus);
    m_close->setToolTip(tr("Close"));
    m_close->setAccessibleName(tr("Close"));
    m_close->setProperty("fxHoverColor", QColor(232, 17, 35, 230));
    h->addWidget(m_icon);
    h->addWidget(m_title);
    h->addStretch(1);
    h->addWidget(m_close);
    setIconName("app-logo");
}

void DialogTitleBar::setTitle(const QString &t) { m_title->setText(t); }

void DialogTitleBar::setIconName(const QString &name) {
    const qreal dpr = devicePixelRatioF();
    m_icon->setPixmap(Icons::pixmap(Icons::path(name), QSize(20, 20), ThemeManager::instance().color("icon-accent", QColor("#6ea0ff")), dpr));
}

void DialogTitleBar::paintEvent(QPaintEvent *) {
    QPainter p(this);
    const auto &t = ThemeManager::instance();
    QLinearGradient g(0, 0, width(), 0);                    // hairline under the bar that fades toward the edges
    QColor line = t.color("border-strong", QColor("#3a4050"));
    QColor clear = line;
    clear.setAlpha(0);
    g.setColorAt(0, clear);
    g.setColorAt(0.12, line);
    g.setColorAt(0.88, line);
    g.setColorAt(1, clear);
    p.setPen(QPen(QBrush(g), 1));
    p.drawLine(0, height() - 1, width(), height() - 1);
}

void DialogTitleBar::mousePressEvent(QMouseEvent *e) {
    if (e->button() == Qt::LeftButton) {
        m_press = e->position().toPoint();
        m_armed = true;
        e->accept();
        return;
    }
    QWidget::mousePressEvent(e);
}

void DialogTitleBar::mouseMoveEvent(QMouseEvent *e) {
    if (m_armed && (e->buttons() & Qt::LeftButton) &&
        (e->position().toPoint() - m_press).manhattanLength() >= QApplication::startDragDistance() && window()->windowHandle()) {
        m_armed = false;
        window()->windowHandle()->startSystemMove();
        e->accept();
        return;
    }
    QWidget::mouseMoveEvent(e);
}

void DialogTitleBar::mouseReleaseEvent(QMouseEvent *e) {
    m_armed = false;
    QWidget::mouseReleaseEvent(e);
}

// ---------------------------------------------------------------------------------------------- StyledDialog
StyledDialog::StyledDialog(QWidget *parent, const QString &icon, bool resizable)
    : QDialog(parent), m_custom(Frameless::customFrameWanted()), m_resizable(resizable) {
    setProperty("frameless", m_custom);
    if (!m_custom) return;                                   // native frame: a plain dialog
    setWindowFlags(Qt::Dialog | Qt::FramelessWindowHint | Qt::WindowCloseButtonHint | Qt::WindowTitleHint);
    setAttribute(Qt::WA_TranslucentBackground);
    setMouseTracking(true);
    setSizeGripEnabled(false);
    setContentsMargins(kInset, kInset + kBar, kInset, kInset);
    m_bar = new DialogTitleBar(this);
    m_bar->setIconName(icon);
    connect(m_bar->closeButton(), &QPushButton::clicked, this, &QDialog::reject);
    connect(this, &QWidget::windowTitleChanged, m_bar, &DialogTitleBar::setTitle);
}

void StyledDialog::setTitleIcon(const QString &name) {
    if (m_bar) m_bar->setIconName(name);
}

void StyledDialog::resizeEvent(QResizeEvent *e) {
    QDialog::resizeEvent(e);
    if (m_bar) {
        m_bar->setGeometry(kInset, kInset, width() - 2 * kInset, kBar);
        m_bar->raise();
    }
    m_backdropSize = {};
}

void StyledDialog::changeEvent(QEvent *e) {
    QDialog::changeEvent(e);
    if (e->type() == QEvent::StyleChange) m_backdropSize = {};
}

void StyledDialog::paintEvent(QPaintEvent *) {
    if (!m_custom) return;
    const QString theme = ThemeManager::instance().current();
    if (m_backdropSize != size() || m_backdropTheme != theme) {
        m_backdrop = Frameless::renderBackdrop(size(), devicePixelRatioF(), kInset, 12, true);
        m_backdropSize = size();
        m_backdropTheme = theme;
    }
    QPainter p(this);
    p.setCompositionMode(QPainter::CompositionMode_Source);
    p.drawPixmap(0, 0, m_backdrop);
}

void StyledDialog::mouseMoveEvent(QMouseEvent *e) {
    if (m_custom && m_resizable) {
        const Qt::Edges edges = Frameless::edgesAt(e->position().toPoint(), size());
        setCursor(edges ? QCursor(Frameless::cursorFor(edges)) : Cursors::arrow());
    }
    QDialog::mouseMoveEvent(e);
}

void StyledDialog::leaveEvent(QEvent *e) {
    if (m_custom) setCursor(Cursors::arrow());
    QDialog::leaveEvent(e);
}

void StyledDialog::mousePressEvent(QMouseEvent *e) {
    if (m_custom && m_resizable && e->button() == Qt::LeftButton) {
        const Qt::Edges edges = Frameless::edgesAt(e->position().toPoint(), size());
        if (edges && windowHandle()) {
            windowHandle()->startSystemResize(edges);
            e->accept();
            return;
        }
    }
    QDialog::mousePressEvent(e);
}

// ------------------------------------------------------------------------------------------------ Msg
namespace {

class MessageDialog : public StyledDialog {
public:
    MessageDialog(QWidget *parent, const QString &title, const QString &text, const QString &icon, const QString &iconToken,
                  QMessageBox::StandardButtons buttons, QMessageBox::StandardButton def)
        : StyledDialog(parent, icon, false) {
        setObjectName("messageDialog");
        setWindowTitle(title);
        setModal(true);
        auto *root = new QVBoxLayout(this);
        root->setContentsMargins(20, 14, 20, 16);
        root->setSpacing(18);
        auto *row = new QHBoxLayout;
        row->setSpacing(16);
        auto *ic = new IconWidget(icon, iconToken, 34);
        row->addWidget(ic, 0, Qt::AlignTop);
        auto *msg = new QLabel(text);
        msg->setWordWrap(true);
        msg->setTextInteractionFlags(Qt::TextSelectableByMouse);
        msg->setMinimumWidth(340);
        msg->setMaximumWidth(520);
        row->addWidget(msg, 1);
        root->addLayout(row);

        auto *bottom = new QHBoxLayout;
        bottom->setSpacing(8);
        bottom->addStretch(1);
        struct Def { QMessageBox::StandardButton b; const char *label; };
        const Def all[] = {{QMessageBox::Yes, "Yes"}, {QMessageBox::No, "No"}, {QMessageBox::Ok, "OK"},
                           {QMessageBox::Cancel, "Cancel"}, {QMessageBox::Close, "Close"}};
        QMessageBox::StandardButton primary = def;
        if (primary == QMessageBox::NoButton)
            primary = (buttons & QMessageBox::Yes) ? QMessageBox::Yes : (buttons & QMessageBox::Ok) ? QMessageBox::Ok : QMessageBox::NoButton;
        for (const Def &d : all) {
            if (!(buttons & d.b)) continue;
            auto *b = new AnimatedButton(tr(d.label));
            b->setMinimumWidth(84);
            if (d.b == primary) {
                b->setObjectName("primary");
                b->setDefault(true);
            }
            connect(b, &QPushButton::clicked, this, [this, which = d.b] {
                m_result = which;
                accept();
            });
            bottom->addWidget(b);
        }
        root->addLayout(bottom);
        m_escape = (buttons & QMessageBox::Cancel) ? QMessageBox::Cancel : (buttons & QMessageBox::No) ? QMessageBox::No
                 : (buttons & QMessageBox::Close) ? QMessageBox::Close : QMessageBox::Ok;
        m_result = m_escape;
    }

    QMessageBox::StandardButton result() const { return m_result; }
    void reject() override {                                 // Esc / the close button
        m_result = m_escape;
        QDialog::reject();
    }

private:
    QMessageBox::StandardButton m_result = QMessageBox::NoButton, m_escape = QMessageBox::NoButton;
};

QMessageBox::StandardButton run(QWidget *parent, const QString &title, const QString &text, const QString &icon,
                                const QString &token, QMessageBox::StandardButtons b, QMessageBox::StandardButton def) {
    MessageDialog d(parent, title, text, icon, token, b, def);
    d.exec();
    return d.result();
}

}  // namespace

namespace Msg {
QMessageBox::StandardButton question(QWidget *parent, const QString &title, const QString &text,
                                     QMessageBox::StandardButtons buttons, QMessageBox::StandardButton def) {
    return run(parent, title, text, "info", "icon-accent", buttons, def);
}
QMessageBox::StandardButton warning(QWidget *parent, const QString &title, const QString &text,
                                    QMessageBox::StandardButtons buttons, QMessageBox::StandardButton def) {
    return run(parent, title, text, "warning", "warning", buttons, def);
}
QMessageBox::StandardButton information(QWidget *parent, const QString &title, const QString &text,
                                        QMessageBox::StandardButtons buttons, QMessageBox::StandardButton def) {
    return run(parent, title, text, "info", "icon-accent", buttons, def);
}
}  // namespace Msg

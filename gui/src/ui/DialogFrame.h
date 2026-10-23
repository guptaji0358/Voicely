#pragma once
// StyledDialog - the base of every dialog window, so dialogs share the look of the main window instead of the stock
// Windows dialog frame: frameless, rounded and shadowed, with the same gradient / glow / floor-grid backdrop and its
// own slim title bar (icon, title, close button; drag it to move, drag the edges to resize).
//
// Subclasses are written as plain QDialogs: set a window title, build a layout on `this` (the frame reserves its
// area through contentsMargins, so the layout automatically sits inside the panel and below the title bar) and call
// exec(). With the "standard Windows title bar" setting (or TTS_NATIVE_FRAME) the dialog falls back to a normal frame.
//
//   Msg::question / warning / information  - drop-in replacements for QMessageBox's static helpers in the same style.
#include <QDialog>
#include <QMessageBox>
#include <QPixmap>

class AnimatedButton;
class QLabel;

class DialogTitleBar : public QWidget {
    Q_OBJECT
public:
    explicit DialogTitleBar(QWidget *parent = nullptr);
    void setTitle(const QString &t);
    void setIconName(const QString &name);
    AnimatedButton *closeButton() const { return m_close; }

protected:
    void paintEvent(QPaintEvent *) override;
    void mousePressEvent(QMouseEvent *) override;
    void mouseMoveEvent(QMouseEvent *) override;
    void mouseReleaseEvent(QMouseEvent *) override;

private:
    QLabel *m_icon, *m_title;
    AnimatedButton *m_close;
    QPoint m_press;
    bool m_armed = false;
};

class StyledDialog : public QDialog {
    Q_OBJECT
public:
    static constexpr int kInset = 16;       // transparent margin that carries the shadow (and the resize grip)
    static constexpr int kBar = 42;         // title bar height

    explicit StyledDialog(QWidget *parent = nullptr, const QString &icon = QStringLiteral("app-logo"), bool resizable = true);
    void setTitleIcon(const QString &name);
    bool customFrame() const { return m_custom; }

protected:
    void paintEvent(QPaintEvent *) override;
    void resizeEvent(QResizeEvent *) override;
    void mouseMoveEvent(QMouseEvent *) override;
    void mousePressEvent(QMouseEvent *) override;
    void leaveEvent(QEvent *) override;
    void changeEvent(QEvent *) override;

private:
    DialogTitleBar *m_bar = nullptr;
    QPixmap m_backdrop;
    QSize m_backdropSize;
    QString m_backdropTheme;
    bool m_custom, m_resizable;
};

namespace Msg {
QMessageBox::StandardButton question(QWidget *parent, const QString &title, const QString &text,
                                     QMessageBox::StandardButtons buttons = QMessageBox::Yes | QMessageBox::No,
                                     QMessageBox::StandardButton defaultButton = QMessageBox::NoButton);
QMessageBox::StandardButton warning(QWidget *parent, const QString &title, const QString &text,
                                    QMessageBox::StandardButtons buttons = QMessageBox::Ok,
                                    QMessageBox::StandardButton defaultButton = QMessageBox::NoButton);
QMessageBox::StandardButton information(QWidget *parent, const QString &title, const QString &text,
                                        QMessageBox::StandardButtons buttons = QMessageBox::Ok,
                                        QMessageBox::StandardButton defaultButton = QMessageBox::NoButton);
}  // namespace Msg

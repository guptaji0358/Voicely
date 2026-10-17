#pragma once
// Custom window frame built only from supported Qt APIs: Qt::FramelessWindowHint, QWindow::startSystemMove /
// startSystemResize (so the OS still performs the actual move/resize/snap), showMaximized/showMinimized.
//
//   ShellWidget  central widget that paints the window backdrop (rounded, shadowed, subtle perspective artwork)
//                and turns mouse presses on its outer ring into system resizes.
//   TitleBar     logo, app name, backend status, command menu and minimize / maximize-restore / close buttons;
//                dragging it moves the window, double-click toggles maximize.
#include <QPixmap>
#include <QWidget>

class AnimatedButton;
class IconWidget;
class QLabel;
class QMenu;
class QToolButton;
class StateIcon;

namespace Frameless {
constexpr int kGrip = 7;          // width of the resize ring in logical pixels
Qt::Edges edgesAt(const QPoint &p, const QSize &size, int grip = kGrip);
Qt::CursorShape cursorFor(Qt::Edges e);
// True when the custom frame should be used: not disabled by TTS_NATIVE_FRAME / the "nativeFrame" setting.
bool customFrameWanted();
// Keeps a restored geometry on a screen that still exists (monitor unplugged, resolution/scale changed).
QRect fitToScreens(const QRect &saved, const QSize &fallback);
}  // namespace Frameless

class ShellWidget : public QWidget {
    Q_OBJECT
public:
    static constexpr int kWindowInset = 16;      // transparent margin that carries the window shadow
    explicit ShellWidget(bool customFrame, QWidget *parent = nullptr);
    void setMaximizedLook(bool maximized);       // no rounded corners / margin while maximized
    bool customFrame() const { return m_custom; }
    int inset() const { return m_inset; }

protected:
    void paintEvent(QPaintEvent *) override;
    void resizeEvent(QResizeEvent *) override;
    void mouseMoveEvent(QMouseEvent *) override;
    void mousePressEvent(QMouseEvent *) override;
    void leaveEvent(QEvent *) override;
    void changeEvent(QEvent *) override;

private:
    void rebuildBackdrop();
    bool m_custom, m_maximized = false;
    int m_inset;
    QPixmap m_backdrop;
    QSize m_backdropSize;
    QString m_backdropTheme;
};

class TitleBar : public QWidget {
    Q_OBJECT
public:
    explicit TitleBar(QWidget *parent = nullptr);
    void setBackendStatus(int kind, const QString &text);   // kind = StateIcon::Kind
    QMenu *commandMenu() const { return m_menu; }
    AnimatedButton *minimizeButton() const { return m_min; }
    AnimatedButton *maximizeButton() const { return m_max; }
    AnimatedButton *closeButton() const { return m_close; }
    void syncWindowState();                                 // swaps the maximize/restore icon

protected:
    void mousePressEvent(QMouseEvent *) override;
    void mouseMoveEvent(QMouseEvent *) override;
    void mouseReleaseEvent(QMouseEvent *) override;
    void mouseDoubleClickEvent(QMouseEvent *) override;

private:
    void toggleMaximize();
    QPoint m_pressPos;
    bool m_dragArmed = false;
    AnimatedButton *m_min, *m_max, *m_close;
    QToolButton *m_menuBtn;
    QMenu *m_menu;
    QLabel *m_name, *m_chip;
    StateIcon *m_state;
};

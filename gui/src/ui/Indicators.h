#pragma once
// Small custom-painted widgets: state icon / spinner, playback level meter, success check,
// smooth progress bar, empty-state placeholder, plus fade/repolish helpers.
#include <QProgressBar>
#include <QVector>
#include <QWidget>

class QPropertyAnimation;
class QTimer;
class QVariantAnimation;

namespace Fx {
// Fades a widget in from transparent. No-op with reduced motion. The opacity effect is removed
// afterwards so the widget renders normally again.
void fadeIn(QWidget *w, int ms = 180);
// Sets a dynamic property and re-evaluates the stylesheet for that widget.
void setProp(QWidget *w, const char *name, const QVariant &value);
}  // namespace Fx

// A themed SVG icon as a widget (so it follows theme changes without manual refresh).
class IconWidget : public QWidget {
    Q_OBJECT
public:
    explicit IconWidget(const QString &iconName, const QString &colorToken = "icon", int size = 16, QWidget *parent = nullptr);
    QSize sizeHint() const override { return QSize(m_size, m_size); }
protected:
    void paintEvent(QPaintEvent *) override;
private:
    QString m_name, m_token;
    int m_size;
};

// Status glyph: Busy rotates (progress.svg), the others show success / warning / error.
class StateIcon : public QWidget {
    Q_OBJECT
public:
    enum class Kind { None, Busy, Success, Warning, Error };
    explicit StateIcon(int size = 18, QWidget *parent = nullptr);
    void setKind(Kind k);
    Kind kind() const { return m_kind; }
    bool spinning() const;
    QSize sizeHint() const override { return QSize(m_size, m_size); }
protected:
    void paintEvent(QPaintEvent *) override;
    void showEvent(QShowEvent *) override;
    void hideEvent(QHideEvent *) override;
private:
    void sync();
    Kind m_kind = Kind::None;
    int m_size;
    qreal m_angle = 0;
    QVariantAnimation *m_spin;
};

// Scrolling bar meter fed with real playback levels (0..1). Bars decay smoothly; nothing moves
// unless levels arrive or a decay is still settling.
class WaveformWidget : public QWidget {
    Q_OBJECT
public:
    explicit WaveformWidget(QWidget *parent = nullptr);
    void pushLevel(qreal level01);
    void setActive(bool playing);
    void clearLevels();
    bool tickerRunning() const;
    QSize sizeHint() const override { return QSize(320, 72); }
    QSize minimumSizeHint() const override { return QSize(120, 48); }
protected:
    void paintEvent(QPaintEvent *) override;
    void hideEvent(QHideEvent *) override;
private:
    void onTick();
    static constexpr int kBars = 56;
    QVector<qreal> m_target, m_shown;
    QTimer *m_tick;
    bool m_active = false;
};

// Draws a check mark inside a ring, then holds and fades out. Used after a completed export.
class SuccessBadge : public QWidget {
    Q_OBJECT
public:
    explicit SuccessBadge(QWidget *parent = nullptr);
    void play();
    QSize sizeHint() const override { return QSize(24, 24); }
protected:
    void paintEvent(QPaintEvent *) override;
    void hideEvent(QHideEvent *) override;
private:
    qreal m_t = 0, m_opacity = 1;
    QVariantAnimation *m_draw, *m_fade;
    QTimer *m_hold;
};

// Progress bar that glides to new values instead of jumping.
class SmoothProgressBar : public QProgressBar {
    Q_OBJECT
public:
    explicit SmoothProgressBar(QWidget *parent = nullptr);
    void setTarget(int value);   // animated
    void resetNow();             // immediate, back to 0
protected:
    void hideEvent(QHideEvent *) override;
private:
    QPropertyAnimation *m_anim;
};

// Centered illustration + title + hint, drawn directly so colours follow the theme.
class EmptyState : public QWidget {
    Q_OBJECT
public:
    EmptyState(const QString &illustrationName, const QString &title, const QString &hint, bool compact, QWidget *parent = nullptr);
    QSize sizeHint() const override { return m_compact ? QSize(260, 64) : QSize(260, 150); }
protected:
    void paintEvent(QPaintEvent *) override;
private:
    QString m_illustration, m_title, m_hint;
    bool m_compact;
};

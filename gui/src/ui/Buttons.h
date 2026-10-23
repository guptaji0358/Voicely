#pragma once
// Buttons with animated hover/press feedback. QSS cannot animate, so the base look comes from the
// stylesheet and a translucent overlay (colours from the theme's @fx-hover / @fx-press tokens) is
// faded in and out on top of it with QVariantAnimation.
#include <QAbstractButton>
#include <QEnterEvent>
#include <QPainter>
#include <QPushButton>
#include <QToolButton>

class QVariantAnimation;

class ButtonFx : public QObject {
    Q_OBJECT
public:
    explicit ButtonFx(QWidget *owner);
    void setHover(bool on) { drive(m_hoverAnim, m_hover, on ? 1.0 : 0.0, 140); }
    void setPressed(bool on) { drive(m_pressAnim, m_press, on ? 1.0 : 0.0, on ? 60 : 120); }
    void reset();                     // snap back to rest (hidden, disabled)
    void paint(QPainter &p) const;    // draws the overlay over the owner's rect
    qreal hoverLevel() const { return m_hover; }
    qreal pressLevel() const { return m_press; }

private:
    void drive(QVariantAnimation *a, qreal &level, qreal to, int ms);
    QWidget *m_w;
    QVariantAnimation *m_hoverAnim, *m_pressAnim;
    qreal m_hover = 0, m_press = 0;
};

template <class Base>
class FxButton : public Base {
public:
    qreal hoverLevel() const { return m_fx.hoverLevel(); }
    qreal pressLevel() const { return m_fx.pressLevel(); }

protected:
    explicit FxButton(QWidget *parent) : Base(parent), m_fx(this) {
        this->setFocusPolicy(Qt::TabFocus);   // keyboard focus ring without a stuck outline after mouse clicks
    }
    void enterEvent(QEnterEvent *e) override { Base::enterEvent(e); m_fx.setHover(true); }
    void leaveEvent(QEvent *e) override { Base::leaveEvent(e); m_fx.setHover(false); m_fx.setPressed(false); }
    void mousePressEvent(QMouseEvent *e) override {
        Base::mousePressEvent(e);
        if (e->button() == Qt::LeftButton) m_fx.setPressed(true);
    }
    void mouseReleaseEvent(QMouseEvent *e) override { Base::mouseReleaseEvent(e); m_fx.setPressed(false); }
    void hideEvent(QHideEvent *e) override { Base::hideEvent(e); m_fx.reset(); }
    void changeEvent(QEvent *e) override {
        Base::changeEvent(e);
        if (e->type() == QEvent::EnabledChange && !this->isEnabled()) m_fx.reset();
    }
    void paintEvent(QPaintEvent *e) override {
        Base::paintEvent(e);
        QPainter p(this);
        m_fx.paint(p);
    }
    ButtonFx m_fx;
};

class AnimatedButton : public FxButton<QPushButton> {
    Q_OBJECT
public:
    explicit AnimatedButton(const QString &text = {}, QWidget *parent = nullptr);
};

// The inline speaker button of the voice row. Idle shows the speaker icon; Loading shows a
// spinner while the preview is synthesised; Speaking shows moving bars while it really plays.
class PreviewButton : public FxButton<QToolButton> {
    Q_OBJECT
public:
    enum class Mode { Idle, Loading, Speaking };
    explicit PreviewButton(QWidget *parent = nullptr);
    Mode mode() const { return m_mode; }
    void setMode(Mode m);

protected:
    void paintEvent(QPaintEvent *e) override;
    void showEvent(QShowEvent *e) override;
    void hideEvent(QHideEvent *e) override;

private:
    void syncAnimation();
    Mode m_mode = Mode::Idle;
    QVariantAnimation *m_phaseAnim;
    qreal m_phase = 0;
};

// Round play / stop button for lists of sounds (the voice library). Idle shows a glossy accent disk with a play
// triangle; while playing the triangle morphs into a stop square, a halo pulses and a ring around the disk fills with
// the playback position (setProgress). Hover grows the disk and lights a halo; keyboard focus draws a ring.
class PlayButton : public QAbstractButton {
    Q_OBJECT
public:
    explicit PlayButton(QWidget *parent = nullptr);
    void setPlaying(bool on);
    bool playing() const { return m_playing; }
    void setProgress(qreal p);                   // 0..1; a negative value hides the ring
    QSize sizeHint() const override { return {50, 50}; }
    QSize minimumSizeHint() const override { return sizeHint(); }

protected:
    void paintEvent(QPaintEvent *) override;
    void enterEvent(QEnterEvent *) override;
    void leaveEvent(QEvent *) override;
    void mousePressEvent(QMouseEvent *) override;
    void mouseReleaseEvent(QMouseEvent *) override;
    void showEvent(QShowEvent *) override;
    void hideEvent(QHideEvent *) override;
    void changeEvent(QEvent *) override;

private:
    void animate(QVariantAnimation *a, qreal from, qreal to, int ms);
    void syncPulse();
    bool m_playing = false;
    qreal m_hover = 0, m_press = 0, m_morph = 0, m_pulse = 0, m_progress = -1;
    QVariantAnimation *m_hoverAnim, *m_pressAnim, *m_morphAnim, *m_pulseAnim;
};

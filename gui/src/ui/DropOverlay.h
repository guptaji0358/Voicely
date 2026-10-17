#pragma once
// Full-window overlay for drag & drop: while something is dragged over the window it fades in a dimmed glass sheet with
// a marching-ants neon outline, a gently bobbing icon and a message ("Drop to import ..."); on drop a ripple expands from
// the drop point and the sheet fades out. A red variant tells the user the dragged item cannot be used. Mouse events
// pass straight through; reduced motion shows the same sheet without the movement.
#include <QWidget>

class QVariantAnimation;

class DropOverlay : public QWidget {
    Q_OBJECT
public:
    explicit DropOverlay(QWidget *parent);
    void showHint(const QString &title, const QString &subtitle, bool acceptable);
    void hideHint();
    void ripple(const QPointF &at);          // call on drop, then hideHint()
    bool active() const { return m_target > 0.5; }

protected:
    void paintEvent(QPaintEvent *) override;

private:
    void drive(qreal to);
    QVariantAnimation *m_fade, *m_march, *m_ripple;
    qreal m_t = 0, m_target = 0, m_dash = 0, m_rip = 1.0;
    QPointF m_ripAt;
    QString m_title, m_sub;
    bool m_ok = true;
};

#pragma once
// FancyMenu - a QMenu that draws itself, so command menus match the app (rounded glass panel, soft shadow, accent
// hover bar, icon / label / shortcut columns) instead of the stock Windows popup. It is still a normal QMenu: actions,
// separators, keyboard navigation and sub-menus behave as usual; only the frame and the item painting are replaced.
#include "Icons.h"
#include "ThemeManager.h"

#include <QAction>
#include <QFontMetrics>
#include <QLinearGradient>
#include <QMenu>
#include <QPainter>
#include <QPainterPath>

class FancyMenu : public QMenu {
public:
    static constexpr int kPad = 9;          // transparent margin around the panel (carries the shadow)
    static constexpr int kIconCol = 38;     // left column that holds the icon

    explicit FancyMenu(QWidget *parent = nullptr) : QMenu(parent) {
        setObjectName("fancyMenu");
        setWindowFlag(Qt::FramelessWindowHint, true);
        setWindowFlag(Qt::NoDropShadowWindowHint, true);
        setAttribute(Qt::WA_TranslucentBackground, true);
        setAttribute(Qt::WA_Hover, true);
    }

    // Icon column only when some item has an icon, so plain text menus are not indented.
    int iconCol() const {
        for (QAction *a : actions())
            if (!a->isSeparator() && !a->icon().isNull()) return kIconCol;
        return 14;
    }

    QSize sizeHint() const override {
        QSize s = QMenu::sizeHint();
        const int col = iconCol();
        const QFontMetrics fm(font());
        int need = 0;
        for (QAction *a : actions()) {
            if (a->isSeparator() || !a->isVisible()) continue;
            const QString sc = a->shortcut().toString(QKeySequence::NativeText);
            need = qMax(need, col + fm.horizontalAdvance(QString(a->text()).remove('&')) +
                                  (sc.isEmpty() ? 0 : 36 + fm.horizontalAdvance(sc)) + 22);
        }
        s.setWidth(qMax(s.width(), need + 2 * kPad + 2));
        return s;
    }

protected:
    void paintEvent(QPaintEvent *) override {
        const ThemeManager &th = ThemeManager::instance();
        QPainter p(this);
        p.setRenderHint(QPainter::Antialiasing);
        const QRectF panel = QRectF(rect()).adjusted(kPad, kPad, -kPad, -kPad);

        // soft shadow
        p.setPen(Qt::NoPen);
        for (int i = kPad; i >= 1; --i) {
            const qreal f = 1.0 - qreal(i) / (kPad + 1);
            p.setBrush(QColor(0, 0, 0, int(70 * f * f / 1.6)));
            p.drawRoundedRect(panel.adjusted(-i, -i + 3, i, i + 3), 12 + i, 12 + i);
        }
        // panel: gradient glass, hairline border, bright top edge
        QLinearGradient g(panel.topLeft(), panel.bottomLeft());
        g.setColorAt(0, th.color("bg-top", QColor("#181c25")).lighter(122));
        g.setColorAt(1, th.color("bg-bottom", QColor("#0e1015")).lighter(150));
        p.setBrush(g);
        p.setPen(QPen(th.color("border-strong", QColor("#3a4050")), 1));
        p.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), 12, 12);
        p.setPen(QPen(th.color("highlight", QColor(255, 255, 255, 26)), 1));
        p.drawLine(QPointF(panel.left() + 14, panel.top() + 1.5), QPointF(panel.right() - 14, panel.top() + 1.5));

        const int col = iconCol();
        const QColor accent = th.color("accent-top", QColor("#6aa1ff"));
        const QColor txt = th.color("text", QColor("#dde1e9"));
        const QColor muted = th.color("text-muted", QColor("#8b93a4"));
        const QColor off = th.color("text-disabled", QColor("#5d6475"));
        for (QAction *a : actions()) {
            if (!a->isVisible()) continue;
            const QRect ar = actionGeometry(a);
            const QRectF row(panel.left() + 5, ar.top(), panel.width() - 10, ar.height());
            if (a->isSeparator()) {
                QLinearGradient sg(row.left() + 8, 0, row.right() - 8, 0);
                QColor line = th.color("border-strong", QColor("#3a4050"));
                QColor clear = line;
                clear.setAlpha(0);
                sg.setColorAt(0, clear);
                sg.setColorAt(0.5, line);
                sg.setColorAt(1, clear);
                p.setPen(QPen(QBrush(sg), 1));
                p.drawLine(QPointF(row.left() + 8, row.center().y()), QPointF(row.right() - 8, row.center().y()));
                continue;
            }
            const bool on = a->isEnabled();
            const bool hot = on && activeAction() == a;
            if (hot) {
                QLinearGradient hg(row.topLeft(), row.topRight());
                QColor c0 = accent, c1 = accent;
                c0.setAlphaF(0.30);
                c1.setAlphaF(0.08);
                hg.setColorAt(0, c0);
                hg.setColorAt(1, c1);
                p.setPen(Qt::NoPen);
                p.setBrush(hg);
                p.drawRoundedRect(row, 8, 8);
                p.setBrush(accent);                                    // accent bar on the left edge
                p.drawRoundedRect(QRectF(row.left() + 1.5, row.top() + 7, 3, row.height() - 14), 1.5, 1.5);
            }
            const QIcon ic = a->icon();
            if (!ic.isNull()) {
                const QRect ir(int(row.left()) + 13, int(row.center().y()) - 8, 16, 16);
                ic.paint(&p, ir, Qt::AlignCenter, !on ? QIcon::Disabled : hot ? QIcon::Active : QIcon::Normal);
            }
            p.setFont(font());
            p.setPen(!on ? off : txt);
            p.drawText(QRectF(row.left() + col, row.top(), row.width() - col - 10, row.height()),
                       Qt::AlignVCenter | Qt::AlignLeft, QString(a->text()).remove('&'));
            const QString sc = a->shortcut().toString(QKeySequence::NativeText);
            if (!sc.isEmpty()) {
                p.setPen(!on ? off : hot ? accent : muted);
                p.drawText(QRectF(row.left(), row.top(), row.width() - 14, row.height()), Qt::AlignVCenter | Qt::AlignRight, sc);
            }
            if (a->menu()) {                                           // sub-menu chevron
                p.setPen(QPen(muted, 1.5, Qt::SolidLine, Qt::RoundCap));
                const QPointF c(row.right() - 14, row.center().y());
                p.drawLine(c + QPointF(-2.5, -4), c + QPointF(1.5, 0));
                p.drawLine(c + QPointF(1.5, 0), c + QPointF(-2.5, 4));
            }
        }
    }
};

#pragma once
// Responsive cursors: interactive controls show a pointing hand, disabled ones the "not allowed" cursor, and the
// window background shows the busy (arrow + spinner) cursor while the engine is working (ShellWidget::setBusyCursor).
// One application-wide event filter applies this to every button, combo box, slider, tab bar and check box when it
// is shown or its enabled state changes, so dialogs and future widgets get it for free.
#include "Controls.h"

#include <QAbstractButton>
#include <QAbstractSlider>
#include <QComboBox>
#include <QDialog>
#include <QEvent>
#include <QObject>
#include <QTabBar>
#include <QWidget>

class CursorPolicy : public QObject {
public:
    explicit CursorPolicy(QObject *parent = nullptr) : QObject(parent) {}

    static bool interactive(const QObject *o) {
        return qobject_cast<const QAbstractButton *>(o) || qobject_cast<const QComboBox *>(o) ||
               qobject_cast<const QAbstractSlider *>(o) || qobject_cast<const QTabBar *>(o);
    }

    bool eventFilter(QObject *o, QEvent *e) override {
        if ((e->type() == QEvent::Show || e->type() == QEvent::EnabledChange || e->type() == QEvent::Polish) && interactive(o)) {
            auto *w = static_cast<QWidget *>(o);
            w->setCursor(w->isEnabled() ? Cursors::pointer() : QCursor(Qt::ForbiddenCursor));
        } else if (e->type() == QEvent::Show && qobject_cast<QDialog *>(o)) {
            auto *w = static_cast<QWidget *>(o);                   // dialogs are top-level, so they do not inherit the shell's cursor
            if (!w->testAttribute(Qt::WA_SetCursor)) w->setCursor(Cursors::arrow());
        }
        return false;
    }
};

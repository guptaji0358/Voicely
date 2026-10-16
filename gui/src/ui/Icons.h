#pragma once
// SVG icons from the Qt resource system (":/icons/<name>.svg", ":/illustrations/<name>.svg").
// The SVGs use stroke="currentColor"; the colour is resolved from the active theme at paint time,
// so icons follow theme changes, and are rendered as vectors (crisp at any scale).
#include <QColor>
#include <QIcon>
#include <QPainter>
#include <QPixmap>
#include <QString>
#include <QStringList>

namespace Icons {

constexpr int kSize = 18;   // standard logical icon size in toolbars and buttons

QString path(const QString &name);   // "play" -> ":/icons/play.svg"

// colorToken: theme token for the normal state ("icon", "icon-accent", "on-accent").
// Hover uses "icon-active", disabled uses "icon-disabled".
QIcon icon(const QString &name, const QString &colorToken = "icon");

// Draws an SVG resource into `rect`, replacing currentColor with `color` (invalid colour = keep as authored).
void paint(QPainter *p, const QString &resourcePath, const QRectF &rect, const QColor &color = QColor());
QPixmap pixmap(const QString &resourcePath, const QSize &size, const QColor &color = QColor(), qreal dpr = 1.0);

// Every icon/illustration in the resource tree that fails to parse (empty when all are fine).
QStringList brokenResources();
// All icon names the UI code asks for (used by tests to prove each resolves).
QStringList requiredIcons();

}  // namespace Icons

/*
 * Multistream Studio - OBS plugin
 *
 * - A "Vertical" 1080x1920 canvas (OBS 31+ canvas API) with three docks:
 *   Vertical (preview, drag/resize sources), Vertical Scenes, Vertical Sources.
 * - Stream outputs for each platform, controlled by the Multistream Studio app
 *   over obs-websocket (vendor "multistream-studio"). All outputs on the same
 *   canvas share one video encoder, so extra platforms cost no extra encoding.
 */

#include <windows.h>
#include <obs-module.h>
#include <obs-frontend-api.h>
#include <graphics/matrix4.h>
#include <graphics/vec3.h>
#include <util/platform.h>
#include <obs-websocket-api.h>

#include <QWidget>
#include <QDockWidget>
#include <QVBoxLayout>
#include <QHBoxLayout>
#include <QListWidget>
#include <QPushButton>
#include <QToolButton>
#include <QLabel>
#include <QMenu>
#include <QInputDialog>
#include <QMessageBox>
#include <QTimer>
#include <QMouseEvent>
#include <QResizeEvent>
#include <QShowEvent>
#include <QWindow>
#include <QComboBox>
#include <QTabWidget>
#include <QAbstractItemView>

#include <map>
#include <mutex>
#include <string>
#include <vector>
#include <algorithm>

OBS_DECLARE_MODULE()
OBS_MODULE_USE_DEFAULT_LOCALE("multistream-studio", "en-US")

#define LOG(level, fmt, ...) blog(level, "[Multistream Studio] " fmt, ##__VA_ARGS__)

static const char *CANVAS_NAME = "Vertical";
static const char *PLUGIN_VERSION = "1.1.0";
static const uint32_t V_WIDTH = 1080, V_HEIGHT = 1920;

/* ------------------------------------------------------------------ canvas */

static obs_canvas_t *g_canvas = nullptr; // strong ref while a scene collection is loaded

static obs_canvas_t *find_vertical_canvas()
{
	obs_frontend_canvas_list list = {};
	obs_frontend_get_canvases(&list);
	obs_canvas_t *found = nullptr;
	for (size_t i = 0; i < list.canvases.num; i++) {
		obs_canvas_t *c = list.canvases.array[i];
		if (strcmp(obs_canvas_get_name(c), CANVAS_NAME) == 0) {
			found = obs_canvas_get_ref(c);
			break;
		}
	}
	obs_frontend_canvas_list_free(&list);
	return found;
}

static bool first_scene_cb(void *param, obs_source_t *src)
{
	auto out = static_cast<obs_source_t **>(param);
	*out = obs_source_get_ref(src);
	return false;
}

static obs_source_t *first_canvas_scene(obs_canvas_t *canvas)
{
	obs_source_t *first = nullptr;
	obs_canvas_enum_scenes(canvas, first_scene_cb, &first);
	return first;
}

static void setup_canvas()
{
	if (g_canvas)
		return;
	obs_video_info ovi = {};
	obs_get_video_info(&ovi);
	ovi.base_width = ovi.output_width = V_WIDTH;
	ovi.base_height = ovi.output_height = V_HEIGHT;

	g_canvas = find_vertical_canvas();
	if (!g_canvas) {
		g_canvas = obs_frontend_add_canvas(CANVAS_NAME, &ovi, PROGRAM);
		LOG(LOG_INFO, "created vertical canvas");
	}
	if (!g_canvas)
		return;
	// OBS saves canvases without their resolution, so set it every time.
	if (!obs_canvas_has_video(g_canvas))
		obs_canvas_reset_video(g_canvas, &ovi);

	obs_source_t *scene = first_canvas_scene(g_canvas);
	if (!scene) {
		obs_scene_t *s = obs_canvas_scene_create(g_canvas, "Vertical Scene");
		scene = obs_source_get_ref(obs_scene_get_source(s));
		obs_scene_release(s);
	}
	obs_source_t *current = obs_canvas_get_channel(g_canvas, 0);
	if (!current)
		obs_canvas_set_channel(g_canvas, 0, scene);
	obs_source_release(current);
	obs_source_release(scene);
}

static std::mutex g_mutex; // guards the outputs/encoders below AND g_canvas against the obs-websocket thread

static void release_canvas()
{
	std::lock_guard<std::mutex> lock(g_mutex);
	obs_canvas_release(g_canvas);
	g_canvas = nullptr;
}

static obs_scene_t *current_vertical_scene() // not referenced; valid on the UI thread
{
	if (!g_canvas)
		return nullptr;
	obs_source_t *src = obs_canvas_get_channel(g_canvas, 0);
	obs_scene_t *scene = src ? obs_scene_from_source(src) : nullptr;
	obs_source_release(src);
	return scene;
}

static obs_sceneitem_t *g_selected = nullptr; // weak: re-validated before use

static bool item_in_scene(obs_scene_t *scene, obs_sceneitem_t *item)
{
	if (!scene || !item)
		return false;
	struct P {
		obs_sceneitem_t *item;
		bool found;
	} p = {item, false};
	obs_scene_enum_items(
		scene,
		[](obs_scene_t *, obs_sceneitem_t *it, void *param) {
			auto pp = static_cast<P *>(param);
			if (it == pp->item)
				pp->found = true;
			return !pp->found;
		},
		&p);
	return p.found;
}

static obs_sceneitem_t *selected_item()
{
	obs_scene_t *scene = current_vertical_scene();
	return item_in_scene(scene, g_selected) ? g_selected : nullptr;
}

/* ------------------------------------------------------------------ preview */

class VerticalPreview : public QWidget {
public:
	explicit VerticalPreview(QWidget *parent = nullptr) : QWidget(parent)
	{
		setAttribute(Qt::WA_PaintOnScreen);
		setAttribute(Qt::WA_NativeWindow);
		setAttribute(Qt::WA_NoSystemBackground);
		setAttribute(Qt::WA_OpaquePaintEvent);
		setMouseTracking(true);
		setMinimumSize(120, 200);
	}
	~VerticalPreview() override { destroyDisplay(); }

	QPaintEngine *paintEngine() const override { return nullptr; }

	void destroyDisplay()
	{
		if (display) {
			obs_display_remove_draw_callback(display, draw, this);
			obs_display_destroy(display);
			display = nullptr;
		}
	}

protected:
	void showEvent(QShowEvent *e) override
	{
		QWidget::showEvent(e);
		createDisplay();
	}
	void resizeEvent(QResizeEvent *e) override
	{
		QWidget::resizeEvent(e);
		if (display) {
			QSize s = pixelSize();
			obs_display_resize(display, s.width(), s.height());
		}
	}

	void mousePressEvent(QMouseEvent *e) override
	{
		vec2 p = toCanvas(e->position());
		obs_sceneitem_t *item = selected_item();
		if (item && nearHandle(item, p)) {
			mode = Resize;
		} else {
			item = hitTest(p);
			select(item);
			mode = item ? Move : None;
		}
		last = p;
	}
	void mouseMoveEvent(QMouseEvent *e) override
	{
		vec2 p = toCanvas(e->position());
		obs_sceneitem_t *item = selected_item();
		if (!item || mode == None || !(e->buttons() & Qt::LeftButton)) {
			setCursor(item && nearHandle(item, p) ? Qt::SizeFDiagCursor : Qt::ArrowCursor);
			return;
		}
		if (obs_sceneitem_locked(item))
			return;
		if (mode == Move) {
			vec2 pos;
			obs_sceneitem_get_pos(item, &pos);
			pos.x += p.x - last.x;
			pos.y += p.y - last.y;
			obs_sceneitem_set_pos(item, &pos);
		} else {
			// Resize from the bottom-right corner, keeping the aspect ratio.
			vec3 tl, br;
			corners(item, tl, br);
			float oldW = br.x - tl.x;
			float newW = std::max(20.0f, p.x - tl.x);
			if (oldW > 1.0f) {
				float f = newW / oldW;
				if (obs_sceneitem_get_bounds_type(item) != OBS_BOUNDS_NONE) {
					vec2 b;
					obs_sceneitem_get_bounds(item, &b);
					b.x *= f;
					b.y *= f;
					obs_sceneitem_set_bounds(item, &b);
				} else {
					vec2 s;
					obs_sceneitem_get_scale(item, &s);
					s.x *= f;
					s.y *= f;
					obs_sceneitem_set_scale(item, &s);
				}
			}
		}
		last = p;
	}
	void mouseReleaseEvent(QMouseEvent *) override { mode = None; }

private:
	enum Mode { None, Move, Resize } mode = None;
	obs_display_t *display = nullptr;
	vec2 last = {};

	QSize pixelSize() const
	{
		qreal r = devicePixelRatioF();
		return QSize(int(width() * r), int(height() * r));
	}

	void createDisplay()
	{
		if (display || !windowHandle())
			return;
		QSize s = pixelSize();
		gs_init_data info = {};
		info.cx = s.width();
		info.cy = s.height();
		info.format = GS_BGRA;
		info.zsformat = GS_ZS_NONE;
		info.window.hwnd = (HWND)winId();
		display = obs_display_create(&info, 0x1b1d23);
		if (display)
			obs_display_add_draw_callback(display, draw, this);
	}

	// Where the 9:16 canvas sits inside the widget (in physical pixels).
	static void layout(uint32_t cx, uint32_t cy, float &scale, float &ox, float &oy)
	{
		scale = std::min(cx / float(V_WIDTH), cy / float(V_HEIGHT));
		ox = (cx - V_WIDTH * scale) / 2.0f;
		oy = (cy - V_HEIGHT * scale) / 2.0f;
	}

	vec2 toCanvas(QPointF pt) const
	{
		QSize s = pixelSize();
		float scale, ox, oy;
		layout(s.width(), s.height(), scale, ox, oy);
		qreal r = devicePixelRatioF();
		vec2 v;
		v.x = float((pt.x() * r - ox) / scale);
		v.y = float((pt.y() * r - oy) / scale);
		return v;
	}

	static void corners(obs_sceneitem_t *item, vec3 &tl, vec3 &br)
	{
		matrix4 m;
		obs_sceneitem_get_box_transform(item, &m);
		vec3 a, b;
		vec3_set(&a, 0, 0, 0);
		vec3_set(&b, 1, 1, 0);
		vec3_transform(&tl, &a, &m);
		vec3_transform(&br, &b, &m);
		if (tl.x > br.x)
			std::swap(tl.x, br.x);
		if (tl.y > br.y)
			std::swap(tl.y, br.y);
	}

	bool nearHandle(obs_sceneitem_t *item, vec2 p) const
	{
		vec3 tl, br;
		corners(item, tl, br);
		return fabsf(p.x - br.x) < 40 && fabsf(p.y - br.y) < 40;
	}

	static obs_sceneitem_t *hitTest(vec2 p)
	{
		obs_scene_t *scene = current_vertical_scene();
		if (!scene)
			return nullptr;
		struct H {
			vec2 p;
			obs_sceneitem_t *hit;
		} h = {p, nullptr};
		// Items enumerate bottom-to-top, so the last hit is the top-most one.
		obs_scene_enum_items(
			scene,
			[](obs_scene_t *, obs_sceneitem_t *it, void *param) {
				auto hh = static_cast<H *>(param);
				if (!obs_sceneitem_visible(it))
					return true;
				vec3 tl, br;
				corners(it, tl, br);
				if (hh->p.x >= tl.x && hh->p.x <= br.x && hh->p.y >= tl.y && hh->p.y <= br.y)
					hh->hit = it;
				return true;
			},
			&h);
		return h.hit;
	}

	static void select(obs_sceneitem_t *item)
	{
		obs_scene_t *scene = current_vertical_scene();
		if (scene)
			obs_scene_enum_items(
				scene,
				[](obs_scene_t *, obs_sceneitem_t *it, void *) {
					obs_sceneitem_select(it, false);
					return true;
				},
				nullptr);
		g_selected = item;
		if (item)
			obs_sceneitem_select(item, true);
	}

	static void drawRect(float x0, float y0, float x1, float y1, uint32_t color, bool fill)
	{
		gs_effect_t *solid = obs_get_base_effect(OBS_EFFECT_SOLID);
		gs_eparam_t *col = gs_effect_get_param_by_name(solid, "color");
		vec4 c;
		vec4_from_rgba(&c, color);
		gs_effect_set_vec4(col, &c);
		while (gs_effect_loop(solid, "Solid")) {
			gs_render_start(true);
			if (fill) {
				gs_vertex2f(x0, y0);
				gs_vertex2f(x1, y0);
				gs_vertex2f(x0, y1);
				gs_vertex2f(x1, y1);
				gs_render_stop(GS_TRISTRIP);
			} else {
				gs_vertex2f(x0, y0);
				gs_vertex2f(x1, y0);
				gs_vertex2f(x1, y1);
				gs_vertex2f(x0, y1);
				gs_vertex2f(x0, y0);
				gs_render_stop(GS_LINESTRIP);
			}
		}
	}

	static void draw(void *, uint32_t cx, uint32_t cy)
	{
		if (!g_canvas)
			return;
		float scale, ox, oy;
		layout(cx, cy, scale, ox, oy);

		gs_viewport_push();
		gs_projection_push();
		gs_set_viewport(int(ox), int(oy), int(V_WIDTH * scale), int(V_HEIGHT * scale));
		gs_ortho(0.0f, float(V_WIDTH), 0.0f, float(V_HEIGHT), -100.0f, 100.0f);

		drawRect(0, 0, V_WIDTH, V_HEIGHT, 0xFF000000, true);
		obs_canvas_render(g_canvas);

		if (obs_sceneitem_t *item = selected_item()) {
			vec3 tl, br;
			corners(item, tl, br);
			const float t = 3.0f / scale; // ~3 screen pixels
			for (float d = 0; d < t; d += 1.0f / scale)
				drawRect(tl.x - d, tl.y - d, br.x + d, br.y + d, 0xFFFF5C7C, false);
			const float h = 14.0f / scale;
			drawRect(br.x - h, br.y - h, br.x + h, br.y + h, 0xFFFFFFFF, true);
		}

		gs_projection_pop();
		gs_viewport_pop();
	}
};

/* ------------------------------------------------------------------ docks */

static QPushButton *small_button(const QString &text, const QString &tip)
{
	auto b = new QPushButton(text);
	b->setToolTip(tip);
	b->setMinimumWidth(30);
	return b;
}

static std::vector<obs_source_t *> canvas_scenes() // referenced
{
	std::vector<obs_source_t *> out;
	if (!g_canvas)
		return out;
	obs_canvas_enum_scenes(
		g_canvas,
		[](void *param, obs_source_t *src) {
			static_cast<std::vector<obs_source_t *> *>(param)->push_back(obs_source_get_ref(src));
			return true;
		},
		&out);
	return out;
}

class ScenesDock : public QWidget {
public:
	ScenesDock()
	{
		auto lay = new QVBoxLayout(this);
		lay->setContentsMargins(4, 4, 4, 4);
		list = new QListWidget;
		lay->addWidget(list);
		auto row = new QHBoxLayout;
		auto add = small_button("+", "Add a vertical scene");
		auto del = small_button("-", "Remove the selected vertical scene");
		auto ren = small_button("Rename", "Rename the selected vertical scene");
		row->addWidget(add);
		row->addWidget(del);
		row->addWidget(ren);
		row->addStretch();
		lay->addLayout(row);

		connect(list, &QListWidget::itemClicked, [](QListWidgetItem *it) {
			for (obs_source_t *s : canvas_scenes()) {
				if (it->text() == obs_source_get_name(s))
					obs_canvas_set_channel(g_canvas, 0, s);
				obs_source_release(s);
			}
		});
		connect(add, &QPushButton::clicked, [this]() {
			bool ok = false;
			QString name = QInputDialog::getText(this, "New vertical scene", "Name:", QLineEdit::Normal,
							     "Vertical Scene " + QString::number(list->count() + 1), &ok);
			if (!ok || name.trimmed().isEmpty() || !g_canvas)
				return;
			obs_scene_t *s = obs_canvas_scene_create(g_canvas, name.trimmed().toUtf8().constData());
			obs_canvas_set_channel(g_canvas, 0, obs_scene_get_source(s));
			obs_scene_release(s);
		});
		connect(del, &QPushButton::clicked, [this]() {
			auto it = list->currentItem();
			if (!it || list->count() <= 1)
				return;
			if (QMessageBox::question(this, "Remove scene", "Remove \"" + it->text() + "\"?") != QMessageBox::Yes)
				return;
			auto scenes = canvas_scenes();
			obs_source_t *keep = nullptr;
			for (obs_source_t *s : scenes) {
				if (it->text() == obs_source_get_name(s))
					obs_canvas_scene_remove(obs_scene_from_source(s));
				else if (!keep)
					keep = s;
			}
			if (keep)
				obs_canvas_set_channel(g_canvas, 0, keep);
			for (obs_source_t *s : scenes)
				obs_source_release(s);
		});
		connect(ren, &QPushButton::clicked, [this]() {
			auto it = list->currentItem();
			if (!it)
				return;
			bool ok = false;
			QString name = QInputDialog::getText(this, "Rename scene", "Name:", QLineEdit::Normal, it->text(), &ok);
			if (!ok || name.trimmed().isEmpty())
				return;
			for (obs_source_t *s : canvas_scenes()) {
				if (it->text() == obs_source_get_name(s))
					obs_source_set_name(s, name.trimmed().toUtf8().constData());
				obs_source_release(s);
			}
		});

		auto timer = new QTimer(this);
		connect(timer, &QTimer::timeout, [this]() { refresh(); });
		timer->start(500);
	}

	void refresh()
	{
		QStringList names;
		for (obs_source_t *s : canvas_scenes()) {
			names << obs_source_get_name(s);
			obs_source_release(s);
		}
		QString current;
		if (obs_scene_t *cur = current_vertical_scene())
			current = obs_source_get_name(obs_scene_get_source(cur));
		if (names != shown) {
			list->clear();
			list->addItems(names);
			shown = names;
		}
		for (int i = 0; i < list->count(); i++) {
			QListWidgetItem *it = list->item(i);
			bool on = it->text() == current;
			if (it->isSelected() != on)
				it->setSelected(on);
			QFont f = it->font();
			f.setBold(on);
			it->setFont(f);
		}
	}

private:
	QListWidget *list;
	QStringList shown;
};

class SourcesDock : public QWidget {
public:
	SourcesDock()
	{
		auto lay = new QVBoxLayout(this);
		lay->setContentsMargins(4, 4, 4, 4);
		list = new QListWidget;
		lay->addWidget(list);
		auto row = new QHBoxLayout;
		auto row2 = new QHBoxLayout;
		auto add = small_button("+ Add", "Add a source to the vertical scene");
		auto del = small_button("Remove", "Remove the selected source from this vertical scene");
		auto up = small_button("▲", "Move up");
		auto down = small_button("▼", "Move down");
		auto props = small_button("Properties", "Open the selected source's properties");
		auto fit = small_button("Fill screen", "Make the selected source fill the vertical screen");
		for (auto b : {add, del, up, down})
			row->addWidget(b);
		row->addStretch();
		for (auto b : {props, fit})
			row2->addWidget(b);
		row2->addStretch();
		lay->addLayout(row);
		lay->addLayout(row2);

		connect(add, &QPushButton::clicked, [this, add]() { showAddMenu(add); });
		connect(del, &QPushButton::clicked, [this]() {
			if (obs_sceneitem_t *it = currentItem())
				obs_sceneitem_remove(it);
		});
		connect(props, &QPushButton::clicked, [this]() {
			if (obs_sceneitem_t *it = currentItem())
				obs_frontend_open_source_properties(obs_sceneitem_get_source(it));
		});
		connect(list, &QListWidget::itemDoubleClicked, [this](QListWidgetItem *) {
			if (obs_sceneitem_t *it = currentItem())
				obs_frontend_open_source_properties(obs_sceneitem_get_source(it));
		});
		connect(fit, &QPushButton::clicked, [this]() {
			if (obs_sceneitem_t *it = currentItem())
				fitToScreen(it);
		});
		connect(up, &QPushButton::clicked, [this]() {
			if (obs_sceneitem_t *it = currentItem())
				obs_sceneitem_set_order(it, OBS_ORDER_MOVE_UP);
		});
		connect(down, &QPushButton::clicked, [this]() {
			if (obs_sceneitem_t *it = currentItem())
				obs_sceneitem_set_order(it, OBS_ORDER_MOVE_DOWN);
		});
		connect(list, &QListWidget::currentRowChanged, [this](int) {
			obs_sceneitem_t *it = currentItem();
			obs_scene_t *scene = current_vertical_scene();
			if (scene)
				obs_scene_enum_items(
					scene,
					[](obs_scene_t *, obs_sceneitem_t *i, void *) {
						obs_sceneitem_select(i, false);
						return true;
					},
					nullptr);
			g_selected = it;
			if (it)
				obs_sceneitem_select(it, true);
		});
		connect(list, &QListWidget::itemChanged, [this](QListWidgetItem *w) {
			if (updating)
				return;
			if (obs_sceneitem_t *it = itemFor(w))
				obs_sceneitem_set_visible(it, w->checkState() == Qt::Checked);
		});

		auto timer = new QTimer(this);
		connect(timer, &QTimer::timeout, [this]() { refresh(); });
		timer->start(400);
	}

	static void fitToScreen(obs_sceneitem_t *it)
	{
		vec2 pos = {0.0f, 0.0f}, bounds = {float(V_WIDTH), float(V_HEIGHT)};
		obs_sceneitem_set_alignment(it, OBS_ALIGN_LEFT | OBS_ALIGN_TOP);
		obs_sceneitem_set_bounds_type(it, OBS_BOUNDS_SCALE_OUTER);
		obs_sceneitem_set_bounds_alignment(it, OBS_ALIGN_CENTER);
		obs_sceneitem_set_bounds(it, &bounds);
		obs_sceneitem_set_pos(it, &pos);
	}

private:
	QListWidget *list;
	std::vector<obs_sceneitem_t *> items; // top-to-bottom, matches the list
	bool updating = false;

	obs_sceneitem_t *itemFor(QListWidgetItem *w)
	{
		int row = list->row(w);
		obs_scene_t *scene = current_vertical_scene();
		if (row < 0 || row >= int(items.size()) || !item_in_scene(scene, items[row]))
			return nullptr;
		return items[row];
	}
	obs_sceneitem_t *currentItem() { return list->currentItem() ? itemFor(list->currentItem()) : nullptr; }

	void refresh()
	{
		std::vector<obs_sceneitem_t *> now;
		obs_scene_t *scene = current_vertical_scene();
		if (scene)
			obs_scene_enum_items(
				scene,
				[](obs_scene_t *, obs_sceneitem_t *it, void *param) {
					static_cast<std::vector<obs_sceneitem_t *> *>(param)->push_back(it);
					return true;
				},
				&now);
		std::reverse(now.begin(), now.end());

		updating = true;
		if (now != items) {
			obs_sceneitem_t *keep = selected_item();
			items = now;
			list->clear();
			for (obs_sceneitem_t *it : items) {
				auto w = new QListWidgetItem(obs_source_get_name(obs_sceneitem_get_source(it)));
				w->setFlags(w->flags() | Qt::ItemIsUserCheckable);
				list->addItem(w);
			}
			for (size_t i = 0; i < items.size(); i++)
				if (items[i] == keep)
					list->setCurrentRow(int(i));
		}
		for (size_t i = 0; i < items.size(); i++) {
			QListWidgetItem *w = list->item(int(i));
			Qt::CheckState want = obs_sceneitem_visible(items[i]) ? Qt::Checked : Qt::Unchecked;
			if (w->checkState() != want)
				w->setCheckState(want);
			QString name = obs_source_get_name(obs_sceneitem_get_source(items[i]));
			if (w->text() != name)
				w->setText(name);
		}
		// Follow selection made by clicking in the preview.
		obs_sceneitem_t *sel = selected_item();
		for (size_t i = 0; i < items.size(); i++)
			if (items[i] == sel && list->currentRow() != int(i))
				list->setCurrentRow(int(i));
		updating = false;
	}

	void showAddMenu(QWidget *anchor)
	{
		obs_scene_t *scene = current_vertical_scene();
		if (!scene)
			return;
		QMenu menu;
		QMenu *existing = menu.addMenu("Add an existing source (camera, game, alerts...)");
		std::vector<obs_source_t *> sources;
		obs_enum_sources(
			[](void *param, obs_source_t *src) {
				static_cast<std::vector<obs_source_t *> *>(param)->push_back(obs_source_get_ref(src));
				return true;
			},
			&sources);
		std::sort(sources.begin(), sources.end(), [](obs_source_t *a, obs_source_t *b) {
			return QString(obs_source_get_name(a)).compare(obs_source_get_name(b), Qt::CaseInsensitive) < 0;
		});
		for (obs_source_t *src : sources) {
			QString name = obs_source_get_name(src);
			existing->addAction(name, [scene, name]() {
				obs_source_t *s = obs_get_source_by_name(name.toUtf8().constData());
				if (s) {
					obs_sceneitem_t *it = obs_scene_add(scene, s);
					if (it && obs_source_get_width(s) > 0)
						fitToScreen(it);
					obs_source_release(s);
				}
			});
		}
		if (sources.empty())
			existing->addAction("(no sources yet)")->setEnabled(false);
		for (obs_source_t *s : sources)
			obs_source_release(s);

		QMenu *fresh = menu.addMenu("Add a new source");
		const char *id = nullptr, *unversioned = nullptr;
		std::vector<std::pair<QString, QString>> types;
		for (size_t i = 0; obs_enum_input_types2(i, &id, &unversioned); i++) {
			uint32_t flags = obs_get_source_output_flags(id);
			if (flags & (OBS_SOURCE_CAP_DISABLED | OBS_SOURCE_DEPRECATED | OBS_SOURCE_CAP_OBSOLETE))
				continue;
			const char *display = obs_source_get_display_name(id);
			if (display)
				types.emplace_back(display, id);
		}
		std::sort(types.begin(), types.end());
		for (auto &t : types) {
			QString typeId = t.second;
			fresh->addAction(t.first, [this, scene, typeId, t]() {
				bool ok = false;
				QString name = QInputDialog::getText(this, "New source", "Name:", QLineEdit::Normal, t.first, &ok);
				if (!ok || name.trimmed().isEmpty())
					return;
				obs_source_t *s = obs_source_create(typeId.toUtf8().constData(), name.trimmed().toUtf8().constData(),
								    nullptr, nullptr);
				if (!s)
					return;
				obs_scene_add(scene, s);
				obs_frontend_open_source_properties(s);
				obs_source_release(s);
			});
		}
		menu.exec(anchor->mapToGlobal(QPoint(0, anchor->height())));
	}
};

// One dock for everything vertical: the preview always shows; a compact bar switches
// scenes; "Edit" opens the Scenes/Sources editor underneath only when it's needed,
// so the dock stays uncluttered while streaming.
class PreviewDock : public QWidget {
public:
	PreviewDock()
	{
		auto lay = new QVBoxLayout(this);
		lay->setContentsMargins(4, 4, 4, 4);
		lay->setSpacing(4);
		preview = new VerticalPreview;
		lay->addWidget(preview, 1);

		auto bar = new QHBoxLayout;
		sceneBox = new QComboBox;
		sceneBox->setToolTip("Vertical scene that's on air");
		sceneBox->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Fixed);
		editBtn = new QPushButton;
		editBtn->setCheckable(true);
		editBtn->setToolTip("Show or hide the vertical scenes and sources editor");
		bar->addWidget(sceneBox, 1);
		bar->addWidget(editBtn);
		lay->addLayout(bar);

		editor = new QTabWidget;
		editor->addTab(new ScenesDock, "Scenes");
		editor->addTab(new SourcesDock, "Sources");
		editor->setCurrentIndex(1);
		auto hint = new QLabel("In the preview: click a source to select it, drag to move, drag the white corner to resize.");
		hint->setWordWrap(true);
		hint->setStyleSheet("color: #8d94a5; font-size: 11px;");
		editorBox = new QWidget;
		auto el = new QVBoxLayout(editorBox);
		el->setContentsMargins(0, 0, 0, 0);
		el->addWidget(editor);
		el->addWidget(hint);
		editorBox->setMinimumHeight(220);
		lay->addWidget(editorBox);

		connect(editBtn, &QPushButton::toggled, [this](bool on) { setEditing(on); });
		connect(sceneBox, &QComboBox::activated, [this](int) {
			QString name = sceneBox->currentText();
			for (obs_source_t *s : canvas_scenes()) {
				if (name == obs_source_get_name(s))
					obs_canvas_set_channel(g_canvas, 0, s);
				obs_source_release(s);
			}
		});
		auto timer = new QTimer(this);
		connect(timer, &QTimer::timeout, [this]() { refreshScenes(); });
		timer->start(500);

		editBtn->setChecked(loadEditing());
		setEditing(editBtn->isChecked());
	}
	VerticalPreview *preview;

private:
	QComboBox *sceneBox;
	QPushButton *editBtn;
	QTabWidget *editor;
	QWidget *editorBox;
	QStringList shown;

	static std::string statePath()
	{
		char *p = obs_module_config_path("editor_open");
		std::string s = p ? p : "";
		bfree(p);
		return s;
	}
	static bool loadEditing() { return os_file_exists(statePath().c_str()); }

	void setEditing(bool on)
	{
		editorBox->setVisible(on);
		editBtn->setText(on ? "Done ▴" : "Edit ▾");
		std::string path = statePath();
		if (on) {
			char *dir = obs_module_config_path("");
			os_mkdirs(dir);
			bfree(dir);
			os_quick_write_utf8_file(path.c_str(), "1", 1, false);
		} else {
			os_unlink(path.c_str());
		}
	}

	void refreshScenes()
	{
		QStringList names;
		for (obs_source_t *s : canvas_scenes()) {
			names << obs_source_get_name(s);
			obs_source_release(s);
		}
		if (names != shown) {
			sceneBox->clear();
			sceneBox->addItems(names);
			shown = names;
		}
		if (obs_scene_t *cur = current_vertical_scene()) {
			int i = sceneBox->findText(obs_source_get_name(obs_scene_get_source(cur)));
			if (i >= 0 && i != sceneBox->currentIndex() && !sceneBox->view()->isVisible())
				sceneBox->setCurrentIndex(i);
		}
	}
};

static PreviewDock *g_preview_dock = nullptr;

/* ------------------------------------------------------------------ outputs */

// Video encoders are pooled by their settings: accounts on the same canvas with the same
// bitrate / speed / output size share one encoder; different settings get their own.
struct VEnc {
	obs_encoder_t *enc = nullptr;
	std::string canvas;
	int bitrate = 0;
	std::string preset;
	uint32_t width = 0, height = 0; // 0 = canvas size
};

struct Target {
	obs_output_t *output = nullptr;
	obs_service_t *service = nullptr;
	std::string canvas;
	std::string enc_key;
	int bitrate = 0;
	std::string preset;
	uint32_t width = 0, height = 0; // actual pixels sent
};

static std::map<std::string, VEnc> g_venc;          // settings key -> encoder
static obs_encoder_t *g_audio = nullptr;
static std::map<std::string, Target> g_targets;      // account id -> output

static const char *pick_video_encoder()
{
	static const char *preferred[] = {"obs_nvenc_h264_tex", "jim_nvenc", "ffmpeg_nvenc", "obs_qsv11_v2",
					  "h264_texture_amf", "obs_x264"};
	for (const char *want : preferred) {
		const char *id = nullptr;
		for (size_t i = 0; obs_enum_encoder_types(i, &id); i++)
			if (strcmp(id, want) == 0)
				return want;
	}
	return "obs_x264";
}

static obs_data_t *video_settings(const char *encoder, int bitrate, const std::string &preset)
{
	obs_data_t *s = obs_data_create();
	obs_data_set_string(s, "rate_control", "CBR");
	obs_data_set_int(s, "bitrate", bitrate);
	obs_data_set_int(s, "keyint_sec", 2);
	obs_data_set_string(s, "profile", "high");
	bool nv = strstr(encoder, "nvenc") != nullptr;
	if (nv) {
		obs_data_set_string(s, "preset2", preset == "fast" ? "p3" : preset == "best" ? "p7" : "p5");
		obs_data_set_string(s, "preset", preset == "fast" ? "p3" : preset == "best" ? "p7" : "p5");
		obs_data_set_int(s, "bf", 2);
	} else if (strcmp(encoder, "obs_x264") == 0) {
		obs_data_set_string(s, "preset", preset == "fast" ? "superfast" : preset == "best" ? "medium" : "veryfast");
	}
	return s;
}

static void canvas_size(const std::string &canvas, uint32_t &w, uint32_t &h)
{
	obs_video_info ovi = {};
	if (canvas == "vertical") {
		w = V_WIDTH;
		h = V_HEIGHT;
	} else if (obs_get_video_info(&ovi)) {
		w = ovi.output_width;
		h = ovi.output_height;
	} else {
		w = 1920;
		h = 1080;
	}
}

static obs_encoder_t *get_video_encoder(const std::string &canvas, int bitrate, const std::string &preset,
					uint32_t width, uint32_t height, std::string &key_out)
{
	std::string key = canvas + "|" + std::to_string(bitrate) + "|" + preset + "|" + std::to_string(width) + "x" +
			  std::to_string(height);
	key_out = key;
	auto it = g_venc.find(key);
	if (it != g_venc.end())
		return it->second.enc;

	video_t *video = nullptr;
	if (canvas == "vertical") {
		if (!g_canvas)
			return nullptr;
		video = obs_canvas_get_video(g_canvas);
	} else {
		video = obs_get_video();
	}
	const char *id = pick_video_encoder();
	obs_data_t *s = video_settings(id, bitrate, preset);
	VEnc e;
	e.enc = obs_video_encoder_create(id, ("Multistream " + key).c_str(), s, nullptr);
	obs_data_release(s);
	if (!e.enc)
		return nullptr;
	obs_encoder_set_video(e.enc, video);
	if (width && height) {
		// GPU downscale for this account only. GPU encoders ignore a scaled size unless
		// GPU scaling is switched on for the encoder (libobs obs-encoder.c).
		obs_encoder_set_gpu_scale_type(e.enc, OBS_SCALE_BICUBIC);
		obs_encoder_set_scaled_size(e.enc, width, height);
	}
	e.canvas = canvas;
	e.bitrate = bitrate;
	e.preset = preset;
	e.width = width;
	e.height = height;
	g_venc[key] = e;
	LOG(LOG_INFO, "encoder %s for %s canvas: %d kbps, %s, %ux%u", id, canvas.c_str(), bitrate, preset.c_str(), width, height);
	return e.enc;
}

// Free encoders no account uses any more.
static void prune_encoders()
{
	for (auto it = g_venc.begin(); it != g_venc.end();) {
		bool used = false;
		for (auto &kv : g_targets)
			if (kv.second.enc_key == it->first && kv.second.output)
				used = true;
		if (!used) {
			obs_encoder_release(it->second.enc);
			it = g_venc.erase(it);
		} else {
			++it;
		}
	}
}

static obs_encoder_t *get_audio_encoder()
{
	if (!g_audio) {
		obs_data_t *s = obs_data_create();
		obs_data_set_int(s, "bitrate", 160);
		g_audio = obs_audio_encoder_create("ffmpeg_aac", "Multistream audio", s, 0, nullptr);
		obs_data_release(s);
		if (g_audio)
			obs_encoder_set_audio(g_audio, obs_get_audio());
	}
	return g_audio;
}

// Why each output last stopped (OBS_OUTPUT_* code), so the app can explain it in plain words.
// Separate mutex: OBS can fire "stop" while g_mutex is held (force_stop).
static std::mutex g_code_mutex;
static std::map<obs_output_t *, long long> g_stop_codes;

static void on_output_stop(void *param, calldata_t *cd)
{
	std::lock_guard<std::mutex> lock(g_code_mutex);
	g_stop_codes[static_cast<obs_output_t *>(param)] = calldata_int(cd, "code");
}

static long long stop_code(obs_output_t *o)
{
	std::lock_guard<std::mutex> lock(g_code_mutex);
	auto it = g_stop_codes.find(o);
	return it == g_stop_codes.end() ? 0 : it->second;
}

static void destroy_target(Target &t)
{
	if (t.output) {
		signal_handler_disconnect(obs_output_get_signal_handler(t.output), "stop", on_output_stop, t.output);
		{
			std::lock_guard<std::mutex> lock(g_code_mutex);
			g_stop_codes.erase(t.output);
		}
		if (obs_output_active(t.output))
			obs_output_force_stop(t.output);
		obs_output_release(t.output);
	}
	obs_service_release(t.service);
	t = Target();
}

static void req_start(obs_data_t *req, obs_data_t *res, void *)
{
	std::lock_guard<std::mutex> lock(g_mutex);
	std::string id = obs_data_get_string(req, "id");
	std::string canvas = obs_data_get_string(req, "canvas");
	std::string server = obs_data_get_string(req, "server");
	std::string key = obs_data_get_string(req, "key");
	if (canvas != "vertical")
		canvas = "main";
	if (id.empty() || server.empty() || key.empty()) {
		obs_data_set_string(res, "error", "id, server and key are required");
		obs_data_set_bool(res, "success", false);
		return;
	}
	if (canvas == "vertical" && !g_canvas) {
		obs_data_set_string(res, "error", "The vertical canvas isn't ready");
		obs_data_set_bool(res, "success", false);
		return;
	}
	destroy_target(g_targets[id]);
	prune_encoders();

	int bitrate = (int)obs_data_get_int(req, "bitrate");
	bitrate = bitrate > 0 ? std::clamp(bitrate, 500, 50000) : 6000;
	std::string preset = obs_data_get_string(req, "preset");
	if (preset != "fast" && preset != "best")
		preset = "quality";
	uint32_t cw, ch, w = (uint32_t)obs_data_get_int(req, "width"), h = (uint32_t)obs_data_get_int(req, "height");
	canvas_size(canvas, cw, ch);
	if (!w || !h || w >= cw || h >= ch || (w & 1) || (h & 1))
		w = h = 0; // only even-sized downscales; otherwise send the canvas as-is

	std::string enc_key;
	obs_encoder_t *venc = get_video_encoder(canvas, bitrate, preset, w, h, enc_key);
	obs_encoder_t *aenc = get_audio_encoder();
	if (!venc || !aenc) {
		obs_data_set_string(res, "error", "Couldn't create the encoder");
		obs_data_set_bool(res, "success", false);
		return;
	}
	Target t;
	t.canvas = canvas;
	t.enc_key = enc_key;
	t.bitrate = bitrate;
	t.preset = preset;
	t.width = w ? w : cw;
	t.height = h ? h : ch;
	obs_data_t *ss = obs_data_create();
	obs_data_set_string(ss, "server", server.c_str());
	obs_data_set_string(ss, "key", key.c_str());
	t.service = obs_service_create("rtmp_custom", ("Multistream " + id).c_str(), ss, nullptr);
	obs_data_release(ss);
	t.output = obs_output_create("rtmp_output", ("Multistream " + id).c_str(), nullptr, nullptr);
	obs_output_set_service(t.output, t.service);
	obs_output_set_video_encoder(t.output, venc);
	obs_output_set_audio_encoder(t.output, aenc, 0);
	obs_output_set_reconnect_settings(t.output, 20, 3);
	signal_handler_connect(obs_output_get_signal_handler(t.output), "stop", on_output_stop, t.output);
	bool ok = obs_output_start(t.output);
	if (!ok) {
		const char *err = obs_output_get_last_error(t.output);
		obs_data_set_string(res, "error", err && *err ? err : "OBS couldn't start this output");
	}
	g_targets[id] = t;
	obs_data_set_bool(res, "success", ok);
	LOG(LOG_INFO, "start %s on %s canvas: %s", id.c_str(), canvas.c_str(), ok ? "ok" : "failed");
}

static void req_stop(obs_data_t *req, obs_data_t *res, void *)
{
	std::lock_guard<std::mutex> lock(g_mutex);
	std::string id = obs_data_get_string(req, "id");
	auto it = g_targets.find(id);
	if (it != g_targets.end()) {
		if (it->second.output && obs_output_active(it->second.output))
			obs_output_stop(it->second.output);
	}
	obs_data_set_bool(res, "success", true);
}

static void req_stop_all(obs_data_t *, obs_data_t *res, void *)
{
	std::lock_guard<std::mutex> lock(g_mutex);
	for (auto &kv : g_targets)
		if (kv.second.output && obs_output_active(kv.second.output))
			obs_output_stop(kv.second.output);
	obs_data_set_bool(res, "success", true);
}

static void req_status(obs_data_t *, obs_data_t *res, void *)
{
	std::lock_guard<std::mutex> lock(g_mutex);
	obs_data_array_t *arr = obs_data_array_create();
	for (auto &kv : g_targets) {
		obs_output_t *o = kv.second.output;
		if (!o)
			continue;
		obs_data_t *d = obs_data_create();
		obs_data_set_string(d, "id", kv.first.c_str());
		obs_data_set_string(d, "canvas", kv.second.canvas.c_str());
		obs_data_set_bool(d, "active", obs_output_active(o));
		obs_data_set_bool(d, "reconnecting", obs_output_reconnecting(o));
		obs_data_set_int(d, "total_bytes", (long long)obs_output_get_total_bytes(o));
		obs_data_set_int(d, "total_frames", obs_output_get_total_frames(o));
		obs_data_set_int(d, "dropped_frames", obs_output_get_frames_dropped(o));
		obs_data_set_double(d, "congestion", obs_output_active(o) ? obs_output_get_congestion(o) : 0.0);
		const char *err = obs_output_get_last_error(o);
		obs_data_set_string(d, "last_error", err ? err : "");
		obs_data_set_int(d, "stop_code", stop_code(o));
		obs_data_set_int(d, "bitrate", kv.second.bitrate);
		obs_data_set_string(d, "preset", kv.second.preset.c_str());
		obs_data_set_int(d, "width", kv.second.width);
		obs_data_set_int(d, "height", kv.second.height);
		obs_data_array_push_back(arr, d);
		obs_data_release(d);
	}
	obs_data_set_array(res, "outputs", arr);
	obs_data_array_release(arr);

	obs_data_set_bool(res, "vertical_ready", g_canvas != nullptr);
	obs_data_set_string(res, "encoder", pick_video_encoder());
	uint32_t mw, mh;
	canvas_size("main", mw, mh);
	obs_video_info ovi = {};
	double fps = obs_get_video_info(&ovi) && ovi.fps_den ? (double)ovi.fps_num / ovi.fps_den : 30.0;
	obs_data_set_int(res, "main_width", mw);
	obs_data_set_int(res, "main_height", mh);
	obs_data_set_int(res, "vertical_width", V_WIDTH);
	obs_data_set_int(res, "vertical_height", V_HEIGHT);
	obs_data_set_double(res, "fps", fps);
	obs_data_set_int(res, "encoders_in_use", (long long)g_venc.size());
	obs_data_set_string(res, "version", PLUGIN_VERSION);
	obs_data_set_bool(res, "success", true);
}

// ---- vertical scene requests (app scene switcher, Stream Deck, tests). Canvas changes run on the UI thread.
struct SceneTask {
	std::string name;
	std::string action; // "switch" | "remove"
	bool ok = false;
};

static void scene_task(void *param)
{
	auto t = static_cast<SceneTask *>(param);
	if (!g_canvas)
		return;
	auto scenes = canvas_scenes();
	obs_source_t *target = nullptr, *other = nullptr;
	for (obs_source_t *src : scenes) {
		if (t->name == obs_source_get_name(src))
			target = src;
		else if (!other)
			other = src;
	}
	if (target && t->action == "switch") {
		obs_canvas_set_channel(g_canvas, 0, target);
		t->ok = true;
	} else if (target && t->action == "remove" && other) { // never remove the last vertical scene
		obs_source_t *cur = obs_canvas_get_channel(g_canvas, 0);
		if (cur == target)
			obs_canvas_set_channel(g_canvas, 0, other);
		obs_source_release(cur);
		obs_canvas_scene_remove(obs_scene_from_source(target));
		t->ok = true;
	}
	for (obs_source_t *src : scenes)
		obs_source_release(src);
}

static void req_vertical_scenes(obs_data_t *, obs_data_t *res, void *)
{
	std::lock_guard<std::mutex> lock(g_mutex); // g_canvas may be released by a scene collection switch
	obs_data_array_t *arr = obs_data_array_create();
	for (obs_source_t *src : canvas_scenes()) {
		obs_data_t *d = obs_data_create();
		obs_data_set_string(d, "name", obs_source_get_name(src));
		obs_data_array_push_back(arr, d);
		obs_data_release(d);
		obs_source_release(src);
	}
	obs_data_set_array(res, "scenes", arr);
	obs_data_array_release(arr);
	obs_source_t *cur = g_canvas ? obs_canvas_get_channel(g_canvas, 0) : nullptr;
	obs_data_set_string(res, "current", cur ? obs_source_get_name(cur) : "");
	obs_source_release(cur);
	obs_data_set_bool(res, "success", true);
}

static void run_scene_task(obs_data_t *req, obs_data_t *res, const char *action)
{
	SceneTask t;
	t.name = obs_data_get_string(req, "name");
	t.action = action;
	obs_queue_task(OBS_TASK_UI, scene_task, &t, true);
	obs_data_set_bool(res, "success", t.ok);
	if (!t.ok)
		obs_data_set_string(res, "error", "No such vertical scene (or it's the last one)");
}

static void req_switch_vertical_scene(obs_data_t *req, obs_data_t *res, void *)
{
	run_scene_task(req, res, "switch");
}

static void req_remove_vertical_scene(obs_data_t *req, obs_data_t *res, void *)
{
	run_scene_task(req, res, "remove");
}

static void req_set_quality(obs_data_t *, obs_data_t *res, void *)
{
	// Quality is now chosen per account in start_output; kept so older app versions don't error.
	obs_data_set_bool(res, "success", true);
}

static void shutdown_outputs()
{
	std::lock_guard<std::mutex> lock(g_mutex);
	for (auto &kv : g_targets)
		destroy_target(kv.second);
	g_targets.clear();
	for (auto &kv : g_venc)
		obs_encoder_release(kv.second.enc);
	g_venc.clear();
	obs_encoder_release(g_audio);
	g_audio = nullptr;
}

/* ------------------------------------------------------------------ lifecycle */

static bool g_docks_added = false;

static void add_docks()
{
	if (g_docks_added)
		return;
	g_docks_added = true;
	g_preview_dock = new PreviewDock;
	obs_frontend_add_dock_by_id("multistream_vertical", "Vertical", g_preview_dock);

	// OBS starts new plugin docks hidden. Show it once, on the very first run;
	// after that OBS remembers whatever the user does with it.
	char *flag = obs_module_config_path("vertical_dock_shown");
	if (flag && !os_file_exists(flag)) {
		auto main = static_cast<QWidget *>(obs_frontend_get_main_window());
		if (auto dock = qobject_cast<QDockWidget *>(g_preview_dock->parentWidget())) {
			dock->setFloating(true);
			dock->setVisible(true);
			dock->resize(380, 720);
			dock->move(main->geometry().center() - QPoint(190, 360));
		}
		char *dir = obs_module_config_path("");
		os_mkdirs(dir);
		bfree(dir);
		os_quick_write_utf8_file(flag, "1", 1, false);
	}
	bfree(flag);
}

// Start the Multistream Studio app (installed by the setup) so the dock works without any
// OBS script. If it's already running, the app sees its port in use and quietly exits.
// The app gets this OBS's config folder (--obs-config-dir) so it finds the WebSocket password
// whether OBS is installed (%APPDATA%\obs-studio) or portable (<obs>\config\obs-studio).
static void launch_app()
{
	wchar_t path[MAX_PATH] = {};
	ExpandEnvironmentStringsW(L"%LOCALAPPDATA%\\Programs\\MultistreamStudio\\MultistreamStudio.exe", path, MAX_PATH);
	if (GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES)
		return; // not installed (e.g. a developer running the app from source)
	std::wstring cmd = L"\"" + std::wstring(path) + L"\"";
	if (char *cfg = os_get_config_path_ptr("obs-studio")) {
		if (wchar_t *wcfg = os_utf8_to_wcs_ptr(cfg, 0)) {
			cmd += L" --obs-config-dir \"" + std::wstring(wcfg) + L"\"";
			bfree(wcfg);
		}
		bfree(cfg);
	}
	std::vector<wchar_t> buf(cmd.begin(), cmd.end());
	buf.push_back(0);
	STARTUPINFOW si = {};
	si.cb = sizeof(si);
	PROCESS_INFORMATION pi = {};
	if (CreateProcessW(path, buf.data(), nullptr, nullptr, FALSE, CREATE_NO_WINDOW | DETACHED_PROCESS, nullptr, nullptr, &si, &pi)) {
		CloseHandle(pi.hThread);
		CloseHandle(pi.hProcess);
		LOG(LOG_INFO, "started the Multistream Studio app");
	}
}

static void on_event(enum obs_frontend_event event, void *)
{
	switch (event) {
	case OBS_FRONTEND_EVENT_FINISHED_LOADING:
		setup_canvas();
		add_docks();
		launch_app();
		break;
	case OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGING:
		shutdown_outputs(); // vertical encoder is tied to this collection's canvas
		g_selected = nullptr;
		release_canvas();
		break;
	case OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGED:
		setup_canvas();
		break;
	case OBS_FRONTEND_EVENT_EXIT:
		shutdown_outputs();
		g_selected = nullptr;
		if (g_preview_dock)
			g_preview_dock->preview->destroyDisplay();
		release_canvas();
		break;
	default:
		break;
	}
}

bool obs_module_load(void)
{
	obs_frontend_add_event_callback(on_event, nullptr);
	LOG(LOG_INFO, "loaded (version %s)", PLUGIN_VERSION);
	return true;
}

void obs_module_post_load(void)
{
	obs_websocket_vendor vendor = obs_websocket_register_vendor("multistream-studio");
	if (!vendor) {
		LOG(LOG_WARNING, "obs-websocket not available; the app can't control outputs");
		return;
	}
	obs_websocket_vendor_register_request(vendor, "status", req_status, nullptr);
	obs_websocket_vendor_register_request(vendor, "start_output", req_start, nullptr);
	obs_websocket_vendor_register_request(vendor, "stop_output", req_stop, nullptr);
	obs_websocket_vendor_register_request(vendor, "stop_all", req_stop_all, nullptr);
	obs_websocket_vendor_register_request(vendor, "set_quality", req_set_quality, nullptr);
	obs_websocket_vendor_register_request(vendor, "vertical_scenes", req_vertical_scenes, nullptr);
	obs_websocket_vendor_register_request(vendor, "switch_vertical_scene", req_switch_vertical_scene, nullptr);
	obs_websocket_vendor_register_request(vendor, "remove_vertical_scene", req_remove_vertical_scene, nullptr);
}

void obs_module_unload(void)
{
	obs_frontend_remove_event_callback(on_event, nullptr);
}

const char *obs_module_name(void)
{
	return "Multistream Studio";
}

const char *obs_module_description(void)
{
	return "Vertical canvas and multistream outputs for Multistream Studio";
}

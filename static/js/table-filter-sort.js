/*
 * Excel-style column filtering + sorting for admin tables. No dependencies beyond
 * Bootstrap 5 (dropdown component + bundled Popper) which every admin page already
 * loads. Opt a table in with `data-filter-sort`; declare columns via `data-col` on
 * `<th>` and `data-value`/`data-label` on the matching `<td>`. Optionally set
 * `data-empty-text` on the `<table>` to customize the "no rows match" placeholder
 * shown when filters exclude every row. See CLAUDE.md / the roster/report pages for
 * the full markup contract.
 */
(function () {
  'use strict';

  var NONE_VALUE = ' none ';
  var NONE_LABEL = '(none)';
  var DEFAULT_EMPTY_TEXT = 'No rows match the current filters.';

  function compareText(a, b) {
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
  }

  function compareValues(a, b, numeric) {
    if (a === NONE_VALUE && b === NONE_VALUE) return 0;
    if (a === NONE_VALUE) return 1;
    if (b === NONE_VALUE) return -1;
    return numeric ? (parseFloat(a) || 0) - (parseFloat(b) || 0) : compareText(a, b);
  }

  function cellValue(td) {
    var v = td ? td.getAttribute('data-value') : null;
    return v === null || v === '' ? NONE_VALUE : v;
  }

  function cellLabel(td, value) {
    if (value === NONE_VALUE) return NONE_LABEL;
    var label = td.getAttribute('data-label');
    if (label !== null && label !== '') return label;
    return (td.textContent || '').trim();
  }

  function TableController(table) {
    this.table = table;
    this.thead = table.querySelector('thead');
    this.tbody = table.querySelector('tbody');
    this.columns = [];
    this.sortState = null; // { index, dir: 'asc'|'desc' }
    this.emptyRow = null;
    this.build();
  }

  TableController.prototype.build = function () {
    var self = this;
    var headerRow = this.thead && this.thead.querySelector('tr');
    if (!headerRow) return;
    var ths = Array.prototype.slice.call(headerRow.children);

    var emptyText = this.table.getAttribute('data-empty-text') || DEFAULT_EMPTY_TEXT;
    var emptyCell = document.createElement('td');
    emptyCell.colSpan = ths.length;
    emptyCell.className = 'text-center text-muted py-4';
    emptyCell.textContent = emptyText;
    this.emptyRow = document.createElement('tr');
    this.emptyRow.className = 'fs-empty-row d-none';
    this.emptyRow.appendChild(emptyCell);

    ths.forEach(function (th, index) {
      var col = th.getAttribute('data-col');
      if (!col) return;

      var sortable = th.getAttribute('data-sortable') === 'true';
      var sortType = th.getAttribute('data-sort-type') || 'text';
      var filterable = th.getAttribute('data-filter') !== 'none';
      var def = { key: col, index: index, th: th, sortable: sortable, sortType: sortType, filterable: filterable, selected: null };
      self.columns.push(def);

      th.classList.add('fs-th');
      var inner = th.querySelector('.th-inner') || th;

      if (sortable) {
        var caret = document.createElement('i');
        caret.className = 'bi bi-caret-down-fill opacity-25 sort-icon ms-1';
        inner.appendChild(caret);
        // Clicking anywhere in the label/caret area sorts; the funnel button and
        // its dropdown are separate click targets sharing the same <th>.
        inner.style.cursor = 'pointer';
        inner.addEventListener('click', function (e) {
          if (e.target.closest('.filter-funnel-btn') || e.target.closest('.dropdown-menu')) return;
          self.toggleSort(def);
        });
      }

      if (filterable) {
        self.wireFilter(def, th);
      }
    });

    this.tbody.appendChild(this.emptyRow);

    this.applyDefaults();
    this.render();
  };

  TableController.prototype.rows = function () {
    // Excludes an empty-state placeholder row (a single <td colspan> spanning the
    // whole table) — that row has no per-column cells to filter/sort and should
    // always stay visible in place rather than being hidden or reordered.
    return Array.prototype.slice.call(this.tbody.querySelectorAll(':scope > tr')).filter(function (tr) {
      return !tr.querySelector(':scope > td[colspan]');
    });
  };

  TableController.prototype.applyDefaults = function () {
    var self = this;
    this.columns.forEach(function (col) {
      var def = col.th.getAttribute('data-filter-default');
      if (def) {
        var vals = self.uniqueValues(col);
        var match = vals.filter(function (v) { return v.value === def; });
        if (match.length) {
          col.selected = { values: [def] };
          self.updateFunnel(col);
        }
      }
    });
  };

  TableController.prototype.uniqueValues = function (col) {
    var seen = {};
    var out = [];
    this.rows().forEach(function (tr) {
      var td = tr.children[col.index];
      var value = cellValue(td);
      if (seen.hasOwnProperty(value)) return;
      seen[value] = true;
      out.push({ value: value, label: cellLabel(td, value) });
    });
    var numeric = col.sortType === 'num';
    out.sort(function (a, b) { return compareValues(a.value, b.value, numeric); });
    return out;
  };

  TableController.prototype.wireFilter = function (col, th) {
    var self = this;
    var wrap = document.createElement('div');
    wrap.className = 'dropdown d-inline-block ms-1';

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'filter-funnel-btn';
    btn.setAttribute('data-bs-toggle', 'dropdown');
    btn.setAttribute('data-bs-auto-close', 'outside');
    btn.innerHTML = '<i class="bi bi-funnel"></i>';

    var menu = document.createElement('div');
    menu.className = 'dropdown-menu filter-dropdown-menu p-2';

    wrap.appendChild(btn);
    wrap.appendChild(menu);
    (th.querySelector('.th-inner') || th).appendChild(wrap);

    col.button = btn;
    col.menu = menu;

    wrap.addEventListener('show.bs.dropdown', function () {
      self.renderMenu(col);
    });
  };

  TableController.prototype.renderMenu = function (col) {
    var self = this;
    var values = this.uniqueValues(col);
    var committed = col.selected ? col.selected.values.slice() : values.map(function (v) { return v.value; });
    var pending = committed.slice();

    col.menu.innerHTML = '';

    var search = document.createElement('input');
    search.type = 'text';
    search.className = 'form-control form-control-sm mb-2';
    search.placeholder = 'Search...';
    col.menu.appendChild(search);

    var actions = document.createElement('div');
    actions.className = 'd-flex justify-content-between mb-2';
    var selectAll = document.createElement('a');
    selectAll.href = '#';
    selectAll.className = 'small';
    selectAll.textContent = 'Select All';
    var clear = document.createElement('a');
    clear.href = '#';
    clear.className = 'small';
    clear.textContent = 'Clear';
    actions.appendChild(selectAll);
    actions.appendChild(clear);
    col.menu.appendChild(actions);

    var list = document.createElement('div');
    list.className = 'filter-checkbox-list';
    col.menu.appendChild(list);

    var rows = values.map(function (v) {
      var row = document.createElement('div');
      row.className = 'form-check';
      var input = document.createElement('input');
      input.type = 'checkbox';
      input.className = 'form-check-input';
      input.checked = pending.indexOf(v.value) !== -1;
      var label = document.createElement('label');
      label.className = 'form-check-label small';
      label.textContent = v.label;
      var id = 'fs-' + col.key + '-' + Math.random().toString(36).slice(2, 8);
      input.id = id;
      label.setAttribute('for', id);
      row.appendChild(input);
      row.appendChild(label);
      list.appendChild(row);

      input.addEventListener('change', function () {
        var i = pending.indexOf(v.value);
        if (input.checked && i === -1) pending.push(v.value);
        if (!input.checked && i !== -1) pending.splice(i, 1);
      });

      return { value: v.value, label: v.label, row: row, input: input };
    });

    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      rows.forEach(function (r) {
        r.row.classList.toggle('d-none', q !== '' && r.label.toLowerCase().indexOf(q) === -1);
      });
    });

    selectAll.addEventListener('click', function (e) {
      e.preventDefault();
      rows.forEach(function (r) {
        if (r.row.classList.contains('d-none')) return;
        r.input.checked = true;
        if (pending.indexOf(r.value) === -1) pending.push(r.value);
      });
    });

    clear.addEventListener('click', function (e) {
      e.preventDefault();
      rows.forEach(function (r) {
        if (r.row.classList.contains('d-none')) return;
        r.input.checked = false;
        var i = pending.indexOf(r.value);
        if (i !== -1) pending.splice(i, 1);
      });
    });

    var footer = document.createElement('div');
    footer.className = 'd-flex justify-content-end gap-2 mt-2';
    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn btn-sm btn-outline-secondary';
    cancelBtn.textContent = 'Cancel';
    var applyBtn = document.createElement('button');
    applyBtn.type = 'button';
    applyBtn.className = 'btn btn-sm btn-primary';
    applyBtn.textContent = 'Apply';
    footer.appendChild(cancelBtn);
    footer.appendChild(applyBtn);
    col.menu.appendChild(footer);

    cancelBtn.addEventListener('click', function () {
      self.closeDropdown(col);
    });

    applyBtn.addEventListener('click', function () {
      col.selected = pending.length >= values.length ? null : { values: pending };
      self.updateFunnel(col);
      self.render();
      self.closeDropdown(col);
    });
  };

  TableController.prototype.closeDropdown = function (col) {
    var dd = bootstrap.Dropdown.getInstance(col.button);
    if (dd) dd.hide();
  };

  TableController.prototype.updateFunnel = function (col) {
    var icon = col.button.querySelector('i');
    if (col.selected) {
      icon.className = 'bi bi-funnel-fill';
      col.button.classList.add('active');
    } else {
      icon.className = 'bi bi-funnel';
      col.button.classList.remove('active');
    }
  };

  TableController.prototype.toggleSort = function (col) {
    var dir = this.sortState && this.sortState.key === col.key && this.sortState.dir === 'asc' ? 'desc' : 'asc';
    this.sortState = { key: col.key, dir: dir };

    this.columns.forEach(function (c) {
      if (!c.sortable) return;
      var icon = c.th.querySelector('.sort-icon');
      if (!icon) return;
      if (c.key === col.key) {
        icon.className = 'sort-icon bi ' + (dir === 'asc' ? 'bi-caret-up-fill' : 'bi-caret-down-fill') + ' ms-1 text-danger';
      } else {
        icon.className = 'sort-icon bi bi-caret-down-fill opacity-25 ms-1';
      }
    });

    this.render();
  };

  TableController.prototype.render = function () {
    var self = this;
    var rows = this.rows();
    var visibleCount = 0;

    rows.forEach(function (tr) {
      var visible = self.columns.every(function (col) {
        if (!col.selected) return true;
        var td = tr.children[col.index];
        return col.selected.values.indexOf(cellValue(td)) !== -1;
      });
      tr.classList.toggle('d-none', !visible);
      if (visible) visibleCount++;
    });

    if (this.sortState) {
      var sortKey = this.sortState.key;
      var col = this.columns.filter(function (c) { return c.key === sortKey; })[0];
      if (col) {
        var numeric = col.sortType === 'num';
        var dirMul = this.sortState.dir === 'asc' ? 1 : -1;
        rows.sort(function (a, b) {
          var av = cellValue(a.children[col.index]);
          var bv = cellValue(b.children[col.index]);
          return compareValues(av, bv, numeric) * dirMul;
        });
        rows.forEach(function (tr) { self.tbody.appendChild(tr); });
      }
    }

    // Real rows exist but the current filter combination matches none of them —
    // show a placeholder instead of leaving the table collapsed to just its header.
    if (this.emptyRow) {
      this.emptyRow.classList.toggle('d-none', !(rows.length > 0 && visibleCount === 0));
      this.tbody.appendChild(this.emptyRow);
    }
  };

  function init(tableEl) {
    return new TableController(tableEl);
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('table[data-filter-sort]').forEach(function (t) { init(t); });
  });

  window.TableFilterSort = { init: init };
})();

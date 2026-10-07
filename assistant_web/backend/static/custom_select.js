(() => {
  const instances = new WeakMap();
  let openInstance = null;
  let nextId = 0;

  function selectLabel(select) {
    const label = select.labels?.[0];
    const heading = label?.querySelector(":scope > span");
    return heading?.textContent?.trim()
      || select.getAttribute("aria-label")
      || label?.textContent?.trim()
      || "항목";
  }

  function enabledOptions(instance) {
    return instance.options.filter((item) => !item.option.disabled && !item.element.hidden);
  }

  function normalizedSearchText(value) {
    return String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, "");
  }

  function filterOptions(instance, value = "") {
    if (!instance.searchInput) return;
    const needle = normalizedSearchText(value);
    let visibleCount = 0;
    instance.options.forEach((item) => {
      const searchText = item.option.dataset.searchTerms || item.option.textContent || "";
      const hidden = Boolean(needle) && !normalizedSearchText(searchText).includes(needle);
      item.element.hidden = hidden;
      if (!hidden && !item.option.disabled) visibleCount += 1;
    });
    instance.emptyMessage.hidden = visibleCount > 0;
    const candidates = enabledOptions(instance);
    setActiveOption(instance, candidates[0] || null);
    positionMenu(instance);
  }

  function setActiveOption(instance, item) {
    instance.options.forEach((candidate) => {
      candidate.element.classList.toggle("is-active", candidate === item);
    });
    if (!item || item.option.disabled || item.element.hidden) {
      instance.activeItem = null;
      instance.trigger.removeAttribute("aria-activedescendant");
      return;
    }
    instance.activeItem = item;
    instance.trigger.setAttribute("aria-activedescendant", item.element.id);
    item.element.scrollIntoView({ block: "nearest" });
  }

  function positionMenu(instance) {
    const rect = instance.trigger.getBoundingClientRect();
    const gap = 6;
    const viewportHeight = document.documentElement.clientHeight;
    const spaceBelow = viewportHeight - rect.bottom - gap;
    const spaceAbove = rect.top - gap;
    const desiredHeight = Math.min(instance.menu.scrollHeight, 280);
    const openAbove = spaceBelow < Math.min(desiredHeight, 160) && spaceAbove > spaceBelow;
    const availableHeight = Math.max(96, (openAbove ? spaceAbove : spaceBelow) - 8);

    instance.menu.style.left = `${Math.round(rect.left)}px`;
    instance.menu.style.width = `${Math.round(rect.width)}px`;
    instance.menu.style.maxHeight = `${Math.min(280, availableHeight)}px`;
    if (openAbove) {
      instance.menu.style.top = "auto";
      instance.menu.style.bottom = `${Math.round(viewportHeight - rect.top + gap)}px`;
      instance.menu.dataset.placement = "top";
    } else {
      instance.menu.style.top = `${Math.round(rect.bottom + gap)}px`;
      instance.menu.style.bottom = "auto";
      instance.menu.dataset.placement = "bottom";
    }
  }

  function closeDropdown(instance = openInstance, restoreFocus = false) {
    if (!instance) return;
    instance.menu.hidden = true;
    instance.wrapper.classList.remove("is-open");
    instance.trigger.setAttribute("aria-expanded", "false");
    instance.trigger.removeAttribute("aria-activedescendant");
    instance.activeItem = null;
    if (openInstance === instance) openInstance = null;
    if (restoreFocus) instance.trigger.focus();
  }

  function openDropdown(instance) {
    if (instance.select.disabled) return;
    if (openInstance && openInstance !== instance) closeDropdown(openInstance);
    instance.menu.hidden = false;
    instance.wrapper.classList.add("is-open");
    instance.trigger.setAttribute("aria-expanded", "true");
    openInstance = instance;
    if (instance.searchInput) {
      instance.searchInput.value = "";
      filterOptions(instance);
    }
    positionMenu(instance);
    setActiveOption(
      instance,
      instance.options.find((item) => item.option.selected && !item.option.disabled)
        || enabledOptions(instance)[0],
    );
    if (instance.searchInput) requestAnimationFrame(() => instance.searchInput.focus());
  }

  function chooseOption(instance, item) {
    if (!item || item.option.disabled) return;
    const changed = instance.select.value !== item.option.value;
    instance.select.value = item.option.value;
    syncSelect(instance.select);
    closeDropdown(instance, true);
    if (changed) {
      instance.select.dispatchEvent(new Event("input", { bubbles: true }));
      instance.select.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  function moveActive(instance, direction) {
    const candidates = enabledOptions(instance);
    if (!candidates.length) return;
    const currentIndex = candidates.indexOf(instance.activeItem);
    const nextIndex = currentIndex < 0
      ? (direction > 0 ? 0 : candidates.length - 1)
      : (currentIndex + direction + candidates.length) % candidates.length;
    setActiveOption(instance, candidates[nextIndex]);
  }

  function renderOptions(instance) {
    instance.optionsList.innerHTML = "";
    instance.options = Array.from(instance.select.options).map((option, index) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "custom-select-option";
      item.id = `${instance.menu.id}-option-${index}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(option.selected));
      item.disabled = option.disabled;
      const itemLabel = document.createElement("span");
      itemLabel.textContent = option.textContent;
      item.appendChild(itemLabel);
      if (option.dataset.detail) {
        const detail = document.createElement("span");
        detail.className = "custom-select-option-meta";
        detail.textContent = option.dataset.detail;
        item.appendChild(detail);
        item.setAttribute("aria-label", `${option.textContent}, ${option.dataset.detail}`);
      }
      item.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 10 3 3 7-7" /></svg>');
      item.addEventListener("pointerdown", (event) => event.stopPropagation());
      item.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        chooseOption(instance, instance.options[index]);
      });
      instance.optionsList.appendChild(item);
      return { element: item, option };
    });
    syncSelect(instance.select);
  }

  function syncSelect(select) {
    const instance = instances.get(select);
    if (!instance) return;
    const selected = select.selectedOptions[0] || select.options[0];
    instance.value.textContent = selected?.textContent || "선택해 주세요";
    instance.trigger.disabled = select.disabled;
    instance.wrapper.classList.toggle("is-disabled", select.disabled);
    instance.options.forEach((item) => {
      const isSelected = item.option === selected;
      item.element.classList.toggle("is-selected", isSelected);
      item.element.setAttribute("aria-selected", String(isSelected));
      item.element.disabled = item.option.disabled;
    });
  }

  function destroySelect(select) {
    const instance = instances.get(select);
    if (!instance) return;
    if (openInstance === instance) closeDropdown(instance);
    instance.menu.remove();
    instances.delete(select);
  }

  function enhanceSelect(select) {
    if (!(select instanceof HTMLSelectElement) || instances.has(select)) return;

    const label = select.labels?.[0] || null;
    const wrapper = document.createElement("div");
    const trigger = document.createElement("button");
    const value = document.createElement("span");
    const menu = document.createElement("div");
    const optionsList = document.createElement("div");
    const menuId = `custom-select-menu-${++nextId}`;
    const listboxId = `${menuId}-listbox`;

    wrapper.className = "custom-select";
    trigger.type = "button";
    trigger.className = "custom-select-trigger";
    trigger.setAttribute("role", "combobox");
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", listboxId);
    trigger.setAttribute("aria-label", selectLabel(select));
    value.className = "custom-select-value";
    trigger.append(value);
    trigger.insertAdjacentHTML("beforeend", '<svg class="custom-select-chevron" viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4" /></svg>');

    menu.id = menuId;
    menu.className = "custom-select-menu";
    menu.hidden = true;
    optionsList.id = listboxId;
    optionsList.className = "custom-select-options";
    optionsList.setAttribute("role", "listbox");
    let searchInput = null;
    let emptyMessage = null;
    if (select.dataset.searchable === "true") {
      const search = document.createElement("div");
      search.className = "custom-select-search";
      search.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/></svg>');
      searchInput = document.createElement("input");
      searchInput.type = "search";
      searchInput.placeholder = "고객사 검색";
      searchInput.autocomplete = "off";
      searchInput.spellcheck = false;
      searchInput.setAttribute("aria-label", `${selectLabel(select)} 검색`);
      search.appendChild(searchInput);
      menu.appendChild(search);
    }
    if (select.dataset.optionsHint) {
      const hint = document.createElement("p");
      hint.className = "custom-select-hint";
      hint.textContent = select.dataset.optionsHint;
      menu.appendChild(hint);
    }
    emptyMessage = document.createElement("p");
    emptyMessage.className = "custom-select-empty";
    emptyMessage.textContent = "일치하는 고객사가 없습니다.";
    emptyMessage.hidden = true;
    menu.append(optionsList, emptyMessage);
    document.body.appendChild(menu);

    select.parentNode.insertBefore(wrapper, select);
    wrapper.append(select, trigger);
    select.classList.add("custom-select-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.hidden = true;

    const instance = {
      select,
      wrapper,
      trigger,
      value,
      menu,
      optionsList,
      searchInput,
      emptyMessage,
      options: [],
      activeItem: null,
      label,
    };
    instances.set(select, instance);
    renderOptions(instance);

    trigger.addEventListener("pointerdown", (event) => event.stopPropagation());
    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (openInstance === instance) closeDropdown(instance);
      else openDropdown(instance);
    });
    trigger.addEventListener("keydown", (event) => {
      if (["ArrowDown", "ArrowUp", "Enter", " ", "Escape", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        event.stopPropagation();
      }
      if (event.key === "Escape") {
        closeDropdown(instance);
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (openInstance !== instance) openDropdown(instance);
        else moveActive(instance, event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Home" || event.key === "End") {
        if (openInstance !== instance) openDropdown(instance);
        const candidates = enabledOptions(instance);
        setActiveOption(instance, event.key === "Home" ? candidates[0] : candidates[candidates.length - 1]);
        return;
      }
      if (event.key === "Enter" || event.key === " ") {
        if (openInstance === instance) chooseOption(instance, instance.activeItem);
        else openDropdown(instance);
      }
    });
    if (searchInput) {
      searchInput.addEventListener("pointerdown", (event) => event.stopPropagation());
      searchInput.addEventListener("input", () => filterOptions(instance, searchInput.value));
      searchInput.addEventListener("keydown", (event) => {
        if (["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(event.key)) {
          event.preventDefault();
          event.stopPropagation();
        }
        if (event.key === "Escape") {
          closeDropdown(instance, true);
        } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          moveActive(instance, event.key === "ArrowDown" ? 1 : -1);
        } else if (event.key === "Enter") {
          chooseOption(instance, instance.activeItem);
        }
      });
    }
    select.addEventListener("change", () => syncSelect(select));
    select.addEventListener("invalid", () => trigger.focus());

    if (label) {
      label.addEventListener("click", (event) => {
        if (wrapper.contains(event.target)) return;
        event.preventDefault();
        trigger.focus();
        openDropdown(instance);
      });
    }
  }

  function enhanceAll(root = document) {
    if (root instanceof HTMLSelectElement) enhanceSelect(root);
    root.querySelectorAll?.("select").forEach(enhanceSelect);
  }

  document.addEventListener("pointerdown", (event) => {
    if (!openInstance) return;
    if (openInstance.wrapper.contains(event.target) || openInstance.menu.contains(event.target)) return;
    closeDropdown(openInstance);
  });
  window.addEventListener("resize", () => closeDropdown(openInstance));
  document.addEventListener("scroll", (event) => {
    if (openInstance && event.target === openInstance.menu) return;
    closeDropdown(openInstance);
  }, true);

  const observer = new MutationObserver((mutations) => {
    const selectsToRender = new Set();
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach((node) => {
        if (node.nodeType === Node.ELEMENT_NODE) enhanceAll(node);
      });
      mutation.removedNodes.forEach((node) => {
        if (node.nodeType !== Node.ELEMENT_NODE) return;
        const removedSelects = node instanceof HTMLSelectElement
          ? [node]
          : Array.from(node.querySelectorAll?.("select") || []);
        removedSelects.forEach((select) => {
          if (!select.isConnected) destroySelect(select);
        });
      });
      const relatedSelect = mutation.target instanceof HTMLSelectElement
        ? mutation.target
        : mutation.target.closest?.("select");
      if (relatedSelect && instances.has(relatedSelect)) selectsToRender.add(relatedSelect);
    });
    selectsToRender.forEach((select) => renderOptions(instances.get(select)));
  });

  globalThis.AssistantCustomSelect = {
    enhanceAll,
    sync(select) {
      if (instances.has(select)) syncSelect(select);
      else enhanceSelect(select);
    },
    syncAll() {
      document.querySelectorAll("select").forEach(syncSelect);
    },
  };

  enhanceAll();
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "selected"] });
})();

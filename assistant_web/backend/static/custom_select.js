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
    return instance.options.filter((item) => !item.option.disabled);
  }

  function setActiveOption(instance, item) {
    if (!item || item.option.disabled) return;
    instance.options.forEach((candidate) => {
      candidate.element.classList.toggle("is-active", candidate === item);
    });
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
    positionMenu(instance);
    setActiveOption(
      instance,
      instance.options.find((item) => item.option.selected && !item.option.disabled)
        || enabledOptions(instance)[0],
    );
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
    instance.menu.innerHTML = "";
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
      item.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 10 3 3 7-7" /></svg>');
      item.addEventListener("pointerdown", (event) => event.stopPropagation());
      item.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        chooseOption(instance, instance.options[index]);
      });
      instance.menu.appendChild(item);
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
    const menuId = `custom-select-menu-${++nextId}`;

    wrapper.className = "custom-select";
    trigger.type = "button";
    trigger.className = "custom-select-trigger";
    trigger.setAttribute("role", "combobox");
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", menuId);
    trigger.setAttribute("aria-label", selectLabel(select));
    value.className = "custom-select-value";
    trigger.append(value);
    trigger.insertAdjacentHTML("beforeend", '<svg class="custom-select-chevron" viewBox="0 0 20 20" aria-hidden="true"><path d="m6 8 4 4 4-4" /></svg>');

    menu.id = menuId;
    menu.className = "custom-select-menu";
    menu.setAttribute("role", "listbox");
    menu.hidden = true;
    document.body.appendChild(menu);

    select.parentNode.insertBefore(wrapper, select);
    wrapper.append(select, trigger);
    select.classList.add("custom-select-native");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    select.hidden = true;

    const instance = { select, wrapper, trigger, value, menu, options: [], activeItem: null, label };
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

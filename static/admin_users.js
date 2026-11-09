
  (function () {
    var cohortSelect = document.getElementById('invite-cohort');
    var courseSelect = document.getElementById('invite-course');
    var trackSelect = document.getElementById('invite-track');
    var courseHelp = document.getElementById('invite-course-help');
    var trackHelp = document.getElementById('invite-track-help');
    if (!cohortSelect) { return; }

    function selectedCohorts() {
      return new Set(Array.prototype.map.call(cohortSelect.selectedOptions, function (option) {
        return option.value;
      }).filter(Boolean));
    }

    function syncGroupedSelect(select, help, enabledText, disabledText) {
      if (!select) { return; }
      var selected = selectedCohorts();
      var hasCohort = selected.size > 0;
      select.disabled = !hasCohort;
      Array.prototype.forEach.call(select.querySelectorAll('optgroup[data-cohort-id]'), function (group) {
        var visible = selected.has(group.getAttribute('data-cohort-id'));
        group.hidden = !visible;
        group.disabled = !visible;
        Array.prototype.forEach.call(group.querySelectorAll('option'), function (option) {
          if (!visible) { option.selected = false; }
        });
      });
      if (help) { help.textContent = hasCohort ? enabledText : disabledText; }
    }

    function syncAssignments() {
      syncGroupedSelect(
        trackSelect,
        trackHelp,
        'Select one track for each selected cohort.',
        'Select one or more cohorts to see their tracks.'
      );
      syncGroupedSelect(
        courseSelect,
        courseHelp,
        'Only courses attached to the selected cohorts are shown.',
        'Select one or more cohorts to see their courses.'
      );
    }

    cohortSelect.addEventListener('change', syncAssignments);
    syncAssignments();
  }());

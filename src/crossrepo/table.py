"""
The data frame the catalog is handed back in.

Separated from the rest of the package because everything here rests on pandas,
which `crossrepo` does not depend on. Importing this module is what pulls pandas
in, and nothing imports it until a table is actually asked for: `crossrepo.list`
and the others reach for it when they are called, and
[](`crossrepo.__getattr__`) serves `crossrepo.Table` the same way. A plain
``import crossrepo`` therefore still costs nothing.
"""

from __future__ import annotations

import pandas as pd


class Table(pd.DataFrame):
    """
    A data frame that shows itself with its text columns left aligned.

    A [](`pandas.DataFrame`) in every other respect -- it inherits from one, so
    every method, every operator and every library that takes a data frame takes
    this. What it adds is one thing: in a notebook, columns holding text are
    drawn left aligned rather than right.

    That is not decoration. pandas right aligns everything, which suits numbers
    and is wrong for the columns a catalog is mostly made of: a description, a
    path, a repository name. Read down a right aligned column of text and the
    eye has no edge to run along, and the one column anybody scans -- what each
    file holds -- is the worst of them.

    Operations return a `Table` too, so the alignment survives a
    [](`pandas.DataFrame.sort_values`) or a column selection and does not have
    to be asked for again. One column of one is an ordinary
    [](`pandas.Series`), there being nothing to align in a single column.

    Examples
    --------

    ```python
    import crossrepo

    crossrepo.list()                      # already one of these
    crossrepo.Table(some_other_frame)     # or wrap your own
    ```

    See Also
    --------
    [](`crossrepo.list`)
    [](`crossrepo.frame`)
    """

    @property
    def _constructor(self):
        """The class pandas rebuilds one of these as, so slicing keeps it."""
        return Table

    def _repr_html_(self):
        """
        Render for a notebook, left aligning the columns that hold text.

        Which columns those are is decided by dtype rather than by name, so a
        table nobody here wrote -- one the caller made, or one left after a
        [](`pandas.DataFrame.groupby`) -- is treated the same way.

        Returns
        -------
        :
            The table as HTML.
        """
        styler = self.style
        styler.set_table_styles(
            {
                name: [{"selector": "", "props": [("text-align", "left")]}]
                for name, dtype in zip(self.columns, self.dtypes)
                if pd.api.types.is_object_dtype(dtype)
            },
            overwrite=False,
        )
        # A styler draws every row it is given. A data frame stops at
        # `display.max_rows`, and a catalog of three thousand files would
        # otherwise arrive in the notebook whole.
        return styler.to_html(
            max_rows=(
                pd.get_option("styler.render.max_rows")
                or pd.get_option("display.max_rows")
            ),
            max_columns=(
                pd.get_option("styler.render.max_columns")
                or pd.get_option("display.max_columns")
            ),
        )
